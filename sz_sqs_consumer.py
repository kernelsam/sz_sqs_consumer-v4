#! /usr/bin/env python3

import concurrent.futures

import argparse
import orjson
import logging
import traceback

import importlib
import sys
import os
import time
import random
import boto3

from senzing import G2Engine, G2Exception, G2EngineFlags
INTERVAL=10000
LONG_RECORD=os.getenv('LONG_RECORD',default=300)

TUPLE_MSG = 0
TUPLE_STARTTIME = 1
TUPLE_EXTENDED = 2

log_format = '%(asctime)s %(message)s'

def queue_arn_to_url(arn):
  fields = arn.split(':')
  #arn:aws:sqs:us-east-1:666853904750:BrianSQS_DeadLetter"
  #https://queue.amazonaws.com/666853904750/BrianSQS_DeadLetter
  return f'https://queue.amazonaws.com/{fields[4]}/{fields[5]}'


def process_msg(engine, msg, info):
  try:
    record = orjson.loads(msg)
    if info:
      response = bytearray()
      engine.addRecordWithInfo(record['DATA_SOURCE'],record['RECORD_ID'],msg, response)
      return response.decode()
    else:
      engine.addRecord(record['DATA_SOURCE'],record['RECORD_ID'],msg)
      return None
  except Exception as err:
    print(f'{err} [{msg}]', file=sys.stderr)
    raise


try:
  log_level_map = {
      "notset": logging.NOTSET,
      "debug": logging.DEBUG,
      "info": logging.INFO,
      "fatal": logging.FATAL,
      "warning": logging.WARNING,
      "error": logging.ERROR,
      "critical": logging.CRITICAL
  }

  log_level_parameter = os.getenv("SENZING_LOG_LEVEL", "info").lower()
  log_level = log_level_map.get(log_level_parameter, logging.INFO)
  logging.basicConfig(format=log_format, level=log_level)


  parser = argparse.ArgumentParser()
  parser.add_argument('-q', '--queue', dest='url', required=False, help='queue url')
  parser.add_argument('-i', '--info', dest='info', action='store_true', default=False, help='produce withinfo messages')
  parser.add_argument('-t', '--debugTrace', dest='debugTrace', action='store_true', default=False, help='output debug trace information')
  args = parser.parse_args()

  engine_config = os.getenv('SENZING_ENGINE_CONFIGURATION_JSON')
  if not engine_config:
    print('The environment variable SENZING_ENGINE_CONFIGURATION_JSON must be set with a proper JSON configuration.', file=sys.stderr)
    print('Please see https://senzing.zendesk.com/hc/en-us/articles/360038774134-G2Module-Configuration-and-the-Senzing-API', file=sys.stderr)
    exit(-1)

  # Initialize the G2Engine
  g2 = G2Engine()
  g2.init("sz_sqs_consumer",engine_config,args.debugTrace)
  logCheckTime = prevTime = time.time()

  senzing_governor = importlib.import_module("senzing_governor")
  governor = senzing_governor.Governor(hint="sz_sqs_consumer")

  sqs = boto3.client('sqs')
  queue_url = args.url
  if not queue_url:
    queue_url = os.getenv('SENZING_SQS_QUEUE_URL')

  max_workers = os.getenv('SENZING_THREADS_PER_PROCESS')

  queue_attrs = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=['All'])
  # for some reason the RedrivePolicy is a string and not part of the dict
  redrive_policy = orjson.loads(queue_attrs['Attributes']['RedrivePolicy'])
  arn_deadletter = redrive_policy['deadLetterTargetArn']
  # boto3 requires the queue URL but only provides the deadletter queue as an ARN
  # this is a hack to convert
  deadletter_url = queue_arn_to_url(arn_deadletter)
  print(f'DeadLetter: {deadletter_url}')


  messages = 0
  threads_per_process = os.getenv('SENZING_THREADS_PER_PROCESS', None)
  if threads_per_process:
    max_workers = int(threads_per_process)

  with concurrent.futures.ThreadPoolExecutor(max_workers) as executor:
    print(f'Threads: {executor._max_workers}')
    futures = {}
    try:
      while True:

        nowTime = time.time()
        if futures:
          done, _ = concurrent.futures.wait(futures, timeout=10, return_when=concurrent.futures.FIRST_COMPLETED)

          for fut in done:
            result = fut.result()
            if result:
              print(result) # we would handle pushing to withinfo queues here BUT that is likely a second future task/executor
            msg = futures.pop(fut)
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=msg[TUPLE_MSG]['ReceiptHandle'])
            messages+=1

            if messages%INTERVAL == 0: # display rate stats
              diff = nowTime-prevTime
              speed = -1
              if diff > 0.0:
                speed = int(INTERVAL / diff)
              print(f'Processed {messages} adds, {speed} records per second')
              prevTime=nowTime

          if nowTime > logCheckTime+(LONG_RECORD/2): # log long running records
            logCheckTime = nowTime

            response = bytearray()
            g2.stats(response)
            print(f'\n{response.decode()}\n')

            numStuck = 0
            numRejected = 0
            for fut, msg in futures.items():
              if not fut.done():
                duration = nowTime-msg[TUPLE_STARTTIME]
                if duration > LONG_RECORD:
                  numStuck += 1
                  record = orjson.loads(msg[TUPLE_MSG]['Body'])
                  times_extended=msg[TUPLE_EXTENDED]+1
                  new_time = times_extended*LONG_RECORD*2
                  # note that if the queue visibility timeout is less than this then change_visibility will error
                  sqs.change_visibility(QueueUrl=queue_url, ReceiptHandle=msg[TUPLE_MSG]['ReceiptHandle'], VisibilityTimeout=new_time)
                  futures[fut][TUPLE_STARTTIME]=(nowTime,times_extended)
                  print(f'Extended visibility ({duration/60:.3g} min, extended {times_extended}: {record["DATA_SOURCE"]} : {record["RECORD_ID"]}')
              if numStuck >= executor._max_workers:
                print(f'All {executor._max_workers} threads are stuck on long running records')

        #Really want something that forces an "I'm alive" to the server
        pauseSeconds = governor.govern()
        # either governor fully triggered or our executor is full
        # not going to get more messages
        if pauseSeconds < 0.0:
          time.sleep(1)
          continue
        if len(futures) >= executor._max_workers:
          time.sleep(1)
          continue
        if pauseSeconds > 0.0:
          time.sleep(pauseSeconds)

        while len(futures) < executor._max_workers:
          try:
            msg = sqs.receive_message(QueueUrl=queue_url,
                                      MessageAttributeNames=[ 'All' ],
                                      VisibilityTimeout=2*LONG_RECORD,
                                      WaitTimeSeconds=0)
            #print(msg)
            if not msg or not msg.get('Messages'):
              if len(futures) == 0:
                time.sleep(.1)
              break
            this_msg = msg['Messages'][0]
            futures[executor.submit(process_msg, g2, this_msg['Body'], args.info)] = (this_msg,time.time(),0)
          except Exception as err:
            print(f'{type(err).__name__}: {err}', file=sys.stderr)
            raise

      print(f'Processed total of {messages} adds')

    except Exception as err:
      print(f'{type(err).__name__}: Shutting down due to error: {err}', file=sys.stderr)
      traceback.print_exc()
      nowTime = time.time()
      for fut, msg in futures.items():
        if not fut.done():
          duration = nowTime-msg[TUPLE_STARTTIME]
          record = orjson.loads(msg[TUPLE_MSG]['Body'])
          print(f'Still processing ({duration/60:.3g} min: {record["DATA_SOURCE"]} : {record["RECORD_ID"]}')
      executor.shutdown()
      exit(-1)

except Exception as err:
  print(err, file=sys.stderr)
  traceback.print_exc()
  exit(-1)

