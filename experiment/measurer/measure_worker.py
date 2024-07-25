# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Module for measurer workers logic."""
import time
import json
from typing import Dict, Optional
from google.cloud import pubsub_v1
from common import logs
from database.models import Snapshot
import experiment.measurer.datatypes as measurer_datatypes
from experiment.measurer import measure_manager

MEASUREMENT_TIMEOUT = 1
GET_FROM_PUB_SUB_QUEUE_TIMEOUT = 3
logger = logs.Logger()  # pylint: disable=invalid-name


class BaseMeasureWorker:
    """Base class for measure worker. Encapsulates core methods that will be
    implemented for Local and Google Cloud measure workers."""

    def __init__(self, config: Dict):
        self.region_coverage = config['region_coverage']

    def get_task_from_request_queue(self):
        """"Get task from request queue"""
        raise NotImplementedError

    def put_result_in_response_queue(self, measured_snapshot, request):
        """Save measurement result in response queue, for the measure manager to
        retrieve"""
        raise NotImplementedError

    def measure_worker_loop(self):
        """Periodically retrieves request from request queue, measure it, and
        put result in response queue"""
        logs.initialize(default_extras={
            'component': 'measurer',
            'subcomponent': 'worker',
        })
        logger.info('Starting one measure worker loop')
        while True:
            # 'SnapshotMeasureRequest', ['fuzzer', 'benchmark', 'trial_id',
            # 'cycle']
            request = self.get_task_from_request_queue()
            logger.info(
                'Measurer worker: Got request %s %s %d %d from request queue',
                request.fuzzer, request.benchmark, request.trial_id,
                request.cycle)
            measured_snapshot = measure_manager.measure_snapshot_coverage(
                request.fuzzer, request.benchmark, request.trial_id,
                request.cycle, self.region_coverage)
            self.put_result_in_response_queue(measured_snapshot, request)
            time.sleep(MEASUREMENT_TIMEOUT)


class LocalMeasureWorker(BaseMeasureWorker):
    """Class that holds implementations of core methods for running a measure
    worker locally."""

    def __init__(self, config: Dict):
        self.request_queue = config['request_queue']
        self.response_queue = config['response_queue']
        super().__init__(config)

    def get_task_from_request_queue(
            self) -> measurer_datatypes.SnapshotMeasureRequest:
        """Get item from request multiprocessing queue, block if necessary until
        an item is available"""
        request = self.request_queue.get(block=True)
        return request

    def put_result_in_response_queue(
            self, measured_snapshot: Optional[Snapshot],
            request: measurer_datatypes.SnapshotMeasureRequest):
        if measured_snapshot:
            logger.info('Put measured snapshot in response_queue')
            self.response_queue.put(measured_snapshot)
        else:
            retry_request = measurer_datatypes.RetryRequest(
                request.fuzzer, request.benchmark, request.trial_id,
                request.cycle)
            self.response_queue.put(retry_request)


class GoogleCloudMeasureWorker(BaseMeasureWorker):  # pylint: disable=too-many-instance-attributes
    """Worker that consumes from a Google Cloud Pub/Sub Queue, instead of a
    multiprocessing queue"""

    def __init__(self, config: Dict):
        super().__init__(config)
        self.request_queue_topic_id = config['request_queue_topic_id']
        self.response_queue_topic_id = config['response_queue_topic_id']
        self.project_id = config['project_id']
        self.experiment = config['experiment']
        self.request_queue_subscription = f"""request-queue-subscription-
            {self.experiment}"""
        self.publisher_client = pubsub_v1.PublisherClient()
        self.subscriber_client = pubsub_v1.SubscriberClient()
        self.subscription_path = self.subscriber_client.subscription_path(
            self.project_id, self.request_queue_subscription)
        self._create_request_queue_subscription()

    def _create_request_queue_subscription(self):
        """Creates a new Pub/Sub subscription for the request queue."""
        topic_path = self.response_queue_topic_id
        subscription = self.subscriber_client.create_subscription(request={
            'name': self.subscription_path,
            'topic': topic_path
        })
        logger.info(f'Subscription {subscription.name} created successfully.')

        return self.subscription_path

    def get_task_from_request_queue(
            self) -> measurer_datatypes.SnapshotMeasureRequest:
        while True:
            response = self.subscriber_client.pull(
                request={
                    'subscription': self.subscription_path,
                    'max_messages': 1
                },
                timeout=GET_FROM_PUB_SUB_QUEUE_TIMEOUT)

            if response.received_messages:
                message = response.received_messages[0]
                ack_ids = [message.ack_id]

                # Acknowledge the received message to remove it from the queue.
                self.subscriber_client.acknowledge(request={
                    'subscription': self.subscription_path,
                    'ack_ids': ack_ids
                })

                return message.message.data

    def put_result_in_response_queue(self, measured_snapshot, request):
        topic_path = self.publisher_client.topic_path(
            self.project_id, self.response_queue_topic_id)
        if measured_snapshot:
            logger.info('Put measured snapshot in response_queue')
            measured_snapshot_encoded = json.dumps(
                measured_snapshot.__dict__).encode('utf-8')
            self.publisher_client.publish(topic_path, measured_snapshot_encoded)
        else:
            retry_request = measurer_datatypes.SnapshotMeasureRequest(
                request.fuzzer, request.benchmark, request.trial_id,
                request.cycle)
            retry_request_encoded = json.dumps(
                retry_request.__dict__).encode('utf-8')
            self.publisher_client.publish(topic_path, retry_request_encoded)
