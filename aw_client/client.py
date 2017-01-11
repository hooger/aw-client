import json
import logging
import socket
import appdirs
import os
import time
import threading
from typing import Optional, List, Union
from queue import Queue

import requests as req

from aw_core.models import Event
from aw_core.dirs import get_data_dir
from . import config

# FIXME: This line is probably badly placed
logging.getLogger("requests").setLevel(logging.WARNING)
logger = logging.getLogger("aw.client")


# TODO: Should probably use OAuth or something

class ActivityWatchClient:
    def __init__(self, client_name: str, testing=False):
        self.testing = testing

        self.buckets = []
        self.session = {}

        self.client_name = client_name + ("-testing" if testing else "")
        self.client_hostname = socket.gethostname()

        self.server_hostname = config["server_hostname"] if not testing else config["testserver_hostname"]
        self.server_port = config["server_port"] if not testing else config["testserver_port"]

        # Setup failed queues dir
        self.data_dir = get_data_dir("aw-client")
        self.failed_queues_dir = os.path.join(self.data_dir, "failed_events")
        if not os.path.exists(self.failed_queues_dir):
            os.makedirs(self.failed_queues_dir)
        self.queue_file = os.path.join(self.failed_queues_dir, self.client_name)

        self.dispatch_thread = PostDispatchThread(self)

    #
    #   Get/Post base requests
    #

    def _post(self, endpoint: str, data: dict) -> Optional[req.Response]:
        headers = {"Content-type": "application/json"}
        url = "http://{}:{}/api/0/{}".format(self.server_hostname, self.server_port, endpoint)
        response = req.post(url, data=json.dumps(data), headers=headers)
        response.raise_for_status()
        return response

    def _get(self, endpoint: str) -> Optional[req.Response]:
        url = "http://{}:{}/api/0/{}".format(self.server_hostname, self.server_port, endpoint)
        response = req.get(url)
        response.raise_for_status()
        return response

    #
    #   Event get/post requests
    #

    def get_events(self, bucket) -> List[Event]:
        endpoint = "buckets/{}/events".format(bucket)
        events = self._get(endpoint).json()
        return [Event(**event) for event in events]

    def send_event(self, bucket, event: Event):
        endpoint = "buckets/{}/events".format(bucket)
        data = event.to_json_dict()
        self.dispatch_thread.add_request(endpoint, data)

    def send_events(self, bucket, events: List[Event], ignore_failed=False):
        endpoint = "buckets/{}/events".format(bucket)
        data = [event.to_json_dict() for event in events]
        self.dispatch_thread.add_request(endpoint, data)

    def replace_last_event(self, bucket, event: Event):
        endpoint = "buckets/{}/events/replace_last".format(bucket)
        data = event.to_json_dict()
        self.dispatch_thread.add_request(endpoint, data)

    #
    #   Bucket get/post requests
    #

    def get_buckets(self):
        return self._get('buckets').json()

    def setup_bucket(self, bucket_id, event_type: str) -> bool:
        self.buckets.append({"bid": bucket_id, "etype": event_type})

    def _create_buckets(self):
        # Check if bucket exists
        buckets = self.get_buckets()
        for bucket in self.buckets:
            if bucket['bid'] in buckets:
                return False  # Don't do anything if bucket already exists
            else:
                # Create bucket
                endpoint = "buckets/{}".format(bucket['bid'])
                data = {
                    'client': self.client_name,
                    'hostname': self.client_hostname,
                    'type': bucket['etype'],
                }
                self._post(endpoint, data)
                return True

    #
    #   Failed request queue handling
    #

    def _queue_failed_request(self, endpoint: str, data: dict):
        # Find failed queue file
        entry = {"endpoint": endpoint, "data": data}
        with open(self.queue_file, "a+") as queue_fp:
            queue_fp.write(json.dumps(entry) + "\n")

    #
    #   Connection methods
    #

    def connect(self):
        if not self.dispatch_thread.is_alive():
            self.dispatch_thread.start()

class PostDispatchThread(threading.Thread):
    def __init__(self, client):
        threading.Thread.__init__(self)
        self.daemon = True
        self.connected = False
        self.client = client
        self.queue = Queue()
        self.failed_request = None

        # If crash when lost connection, queue failed requests
        # Issue: You know
        failed_requests = []
        with open(self.client.queue_file, "r") as queue_fp:
            for request in queue_fp:
                failed_requests.append(json.loads(request))
            if len(failed_requests) > 0:
                logger.info("Queuing {} failed events: {}".format(len(failed_requests), failed_requests))
                for request in failed_requests:
                    self.add_request(request['endpoint'], request['data'])
                open(self.client.queue_file, "w").close()  # Clear file
        open(self.client.queue_file, "w").close()  # Clear file

    def run(self):
        while True:
            while not self.connected:
                try:
                    # Try to connect
                    self.client._create_buckets()
                    self.connected = True
                    logger.warning("Connection to aw-server established")

                except req.RequestException as e:
                    # If unable to connect, retry in 10s
                    time.sleep(10)

            while self.connected:
                try:
                    if self.failed_request:
                        request = self.failed_request
                        self.failed_request = None
                    else:
                        request = self.queue.get()
                    endpoint = request[0]
                    data = request[1]

                    self.client._post(endpoint, data)
                except req.RequestException as e:
                    self.connected = False
                    self.failed_request = request
                    logger.warning("Can't connect to aw-server, will queue events until connection is available")

    def add_request(self, endpoint, data):
        self.queue.put([endpoint, data])
        if not self.connected:
            self.client._queue_failed_request(endpoint, data)

