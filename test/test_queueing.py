import asyncio
import os
from collections import deque
from unittest.mock import MagicMock

import pytest

from gradio.queueing import Event, Queue
from fastapi import Request

os.environ["GRADIO_ANALYTICS_ENABLED"] = "False"


class AsyncMock(MagicMock):
    async def __call__(self, *args, **kwargs):
        return super(AsyncMock, self).__call__(*args, **kwargs)


@pytest.fixture()
def queue() -> Queue:
    queue_object = Queue(
        live_updates=True,
        concurrency_count=1,
        update_intervals=1,
        max_size=None,
        blocks_dependencies=[],
    )
    yield queue_object
    queue_object.close()


@pytest.fixture()
def mock_event() -> Event:
    event = Event(
        session_hash="test",
        fn_index=0,
        request=Request({"type": "http", "method": "GET"}),
        username=None,
    )
    yield event


class TestQueueMethods:
    def test_start(self, queue: Queue):
        queue.start()
        assert queue.stopped is False
        assert queue.get_active_worker_count() == 0

    def test_stop_resume(self, queue: Queue):
        queue.start()
        queue.close()
        assert queue.stopped
        queue.resume()
        assert queue.stopped is False

    @pytest.mark.asyncio
    async def test_receive(self, queue: Queue, mock_event: Event):
        async def add_data_to_event():
            mock_event.data = ["test"]

        asyncio.create_task(add_data_to_event())

        received_data = await queue.get_data(mock_event)
        assert received_data

    @pytest.mark.asyncio
    async def test_receive_timeout(self, queue: Queue, mock_event: Event):
        async def add_data_to_event():
            await asyncio.sleep(1)
            mock_event.data = ["test"]

        asyncio.create_task(add_data_to_event())

        received_data = await queue.get_data(mock_event, timeout=0.5)
        assert received_data is False

    @pytest.mark.asyncio
    async def test_send(self, queue: Queue, mock_event: Event):
        queue.send_message(mock_event, "process_starts")
        message = mock_event.message_queue.get(timeout=1)
        assert message["msg"] == "process_starts"

    @pytest.mark.asyncio
    async def test_add_to_queue(self, queue: Queue, mock_event: Event):
        queue.push(mock_event)
        assert len(queue.event_queue) == 1

    @pytest.mark.asyncio
    async def test_add_to_queue_with_max_size(self, queue: Queue, mock_event: Event):
        queue.max_size = 1
        queue.push(mock_event)
        assert len(queue.event_queue) == 1
        queue.push(mock_event)
        assert len(queue.event_queue) == 1

    @pytest.mark.asyncio
    async def test_clean_event(self, queue: Queue, mock_event: Event):
        queue.push(mock_event)
        await queue.clean_event(mock_event)
        assert len(queue.event_queue) == 0

    @pytest.mark.asyncio
    async def test_gather_event_data(self, queue: Queue, mock_event: Event):
        async def add_data_to_event():
            mock_event.data = {"data": ["test"], "fn": 0}

        asyncio.create_task(add_data_to_event())

        assert await queue.get_data(mock_event)
        assert mock_event.data == {"data": ["test"], "fn": 0}


    @pytest.mark.asyncio
    async def test_gather_event_data_timeout(self, queue: Queue, mock_event: Event):
        async def take_too_long():
            await asyncio.sleep(1)

        queue.send_message = AsyncMock()
        queue.send_message.return_value = True

        mock_event.websocket.receive_json = take_too_long
        is_awake = await queue.gather_event_data(mock_event, receive_timeout=0.5)
        assert not is_awake

        # Have to use awful [1][0][1] syntax cause of python 3.7
        assert queue.send_message.call_args_list[1][0][1] == {
            "msg": "process_completed",
            "output": {"error": "Time out uploading data to server"},
            "success": False,
        }


class TestQueueEstimation:
    def test_get_update_estimation(self, queue: Queue):
        queue.update_estimation(5)
        estimation = queue.get_estimation()
        assert estimation.avg_event_process_time == 5

        queue.update_estimation(15)
        estimation = queue.get_estimation()
        assert estimation.avg_event_process_time == 10  # (5 + 15) / 2

        queue.update_estimation(100)
        estimation = queue.get_estimation()
        assert estimation.avg_event_process_time == 40  # (5 + 15 + 100) / 3

    @pytest.mark.asyncio
    async def test_send_estimation(self, queue: Queue, mock_event: Event):
        queue.send_message = AsyncMock()
        queue.send_message.return_value = True
        estimation = queue.get_estimation()
        estimation = await queue.send_estimation(mock_event, estimation, 1)
        assert queue.send_message.called
        assert estimation.rank == 1

        queue.update_estimation(5)
        estimation = await queue.send_estimation(mock_event, estimation, 2)
        assert estimation.rank == 2
        assert estimation.rank_eta == 15

    @pytest.mark.asyncio
    async def queue_sets_concurrency_count(self):
        queue_object = Queue(
            live_updates=True,
            concurrency_count=5,
            update_intervals=1,
            max_size=None,
        )
        assert len(queue_object.active_jobs) == 5
        queue_object.close()


class TestQueueProcessEvents:
    @pytest.mark.asyncio
    async def test_process_event(self, queue: Queue, mock_event: Event):
        queue.gather_event_data = AsyncMock()
        queue.gather_event_data.return_value = True
        queue.send_message = AsyncMock()
        queue.send_message.return_value = True
        queue.call_prediction = AsyncMock()
        queue.call_prediction.return_value = {"is_generating": False}
        mock_event.disconnect = AsyncMock()
        queue.clean_event = AsyncMock()
        queue.reset_iterators = AsyncMock()

        queue.active_jobs = [[mock_event]]
        await queue.process_events([mock_event], batch=False)

        queue.call_prediction.assert_called_once()
        mock_event.disconnect.assert_called_once()
        queue.clean_event.assert_called_once()
        queue.reset_iterators.assert_called_with(
            mock_event.session_hash,
            mock_event.fn_index,
        )

    @pytest.mark.asyncio
    async def test_process_event_handles_error_when_gathering_data(
        self, queue: Queue, mock_event: Event
    ):
        mock_event.websocket.send_json = AsyncMock()
        mock_event.websocket.send_json.side_effect = ValueError("Can't connect")
        queue.call_prediction = AsyncMock()
        mock_event.disconnect = AsyncMock()
        queue.clean_event = AsyncMock()
        queue.reset_iterators = AsyncMock()
        mock_event.data = None

        queue.active_jobs = [[mock_event]]
        await queue.process_events([mock_event], batch=False)

        assert not queue.call_prediction.called
        assert queue.clean_event.call_count >= 1

    @pytest.mark.asyncio
    async def test_process_event_handles_error_sending_process_start_msg(
        self, queue: Queue, mock_event: Event
    ):
        mock_event.websocket.send_json = AsyncMock()
        mock_event.websocket.receive_json.return_value = {"data": ["test"], "fn": 0}

        mock_event.websocket.send_json.side_effect = ["2", ValueError("Can't connect")]
        queue.call_prediction = AsyncMock()
        mock_event.disconnect = AsyncMock()
        queue.clean_event = AsyncMock()
        queue.reset_iterators = AsyncMock()
        mock_event.data = None

        queue.active_jobs = [[mock_event]]
        await queue.process_events([mock_event], batch=False)

        assert not queue.call_prediction.called
        assert queue.clean_event.call_count >= 1

    @pytest.mark.asyncio
    async def test_process_event_handles_exception_in_call_prediction_request(
        self, queue: Queue, mock_event: Event
    ):
        mock_event.disconnect = AsyncMock()
        queue.gather_event_data = AsyncMock(return_value=True)
        queue.clean_event = AsyncMock()
        queue.send_message = AsyncMock(return_value=True)
        queue.call_prediction = AsyncMock(side_effect=ValueError("foo"))
        queue.reset_iterators = AsyncMock()

        queue.active_jobs = [[mock_event]]
        await queue.process_events([mock_event], batch=False)

        queue.call_prediction.assert_called_once()
        mock_event.disconnect.assert_called_once()
        assert queue.clean_event.call_count >= 1

    @pytest.mark.asyncio
    async def test_process_event_handles_exception_in_is_generating_request(
        self, queue: Queue, mock_event: Event
    ):
        # We need to return a good response with is_generating=True first,
        # setting up the function to expect further iterative responses.
        # Then we provide a 500 response.
        side_effects = [
            {"is_generating": True},
            Exception("Foo"),
        ]
        mock_event.disconnect = AsyncMock()
        queue.gather_event_data = AsyncMock(return_value=True)
        queue.clean_event = AsyncMock()
        queue.send_message = AsyncMock(return_value=True)
        queue.call_prediction = AsyncMock(side_effect=side_effects)
        queue.reset_iterators = AsyncMock()

        queue.active_jobs = [[mock_event]]
        await queue.process_events([mock_event], batch=False)
        queue.send_message.assert_called_with(
            mock_event,
            {
                "msg": "process_completed",
                "output": {"error": "Foo"},
                "success": False,
            },
        )

        assert queue.call_prediction.call_count == 2
        mock_event.disconnect.assert_called_once()
        assert queue.clean_event.call_count >= 1

    @pytest.mark.asyncio
    async def test_process_event_handles_error_sending_process_completed_msg(
        self, queue: Queue, mock_event: Event
    ):
        mock_event.websocket.receive_json.return_value = {"data": ["test"], "fn": 0}
        mock_event.websocket.send_json = AsyncMock()
        mock_event.websocket.send_json.side_effect = [
            "2",
            "3",
            ValueError("Can't connect"),
        ]
        queue.call_prediction = AsyncMock(return_value={"is_generating": False})
        mock_event.disconnect = AsyncMock()
        queue.clean_event = AsyncMock()
        queue.reset_iterators = AsyncMock()
        mock_event.data = None

        queue.active_jobs = [[mock_event]]
        await queue.process_events([mock_event], batch=False)

        queue.call_prediction.assert_called_once()
        mock_event.disconnect.assert_called_once()
        assert queue.clean_event.call_count >= 1

    @pytest.mark.asyncio
    async def test_process_event_handles_exception_during_disconnect(
        self, queue: Queue, mock_event: Event
    ):
        mock_event.websocket.receive_json.return_value = {"data": ["test"], "fn": 0}
        mock_event.websocket.send_json = AsyncMock()
        queue.call_prediction = AsyncMock(return_value={"is_generating": False})
        queue.reset_iterators = AsyncMock()
        # No exception should be raised during `process_event`
        mock_event.disconnect = AsyncMock(side_effect=ValueError("..."))
        queue.clean_event = AsyncMock()
        mock_event.data = None
        queue.active_jobs = [[mock_event]]
        await queue.process_events([mock_event], batch=False)
        queue.reset_iterators.assert_called_with(
            mock_event.session_hash,
            mock_event.fn_index,
        )


class TestQueueBatch:
    @pytest.mark.asyncio
    async def test_process_event(self, queue: Queue, mock_event: Event):
        queue.gather_event_data = AsyncMock()
        queue.gather_event_data.return_value = True
        queue.send_message = AsyncMock()
        queue.send_message.return_value = True
        queue.call_prediction = AsyncMock()
        queue.call_prediction.return_value = {
            "is_generating": False,
            "data": [[1, 2]],
        }
        mock_event.disconnect = AsyncMock()
        queue.clean_event = AsyncMock()
        queue.reset_iterators = AsyncMock()

        websocket = MagicMock()
        mock_event2 = Event(websocket=websocket, session_hash="test", fn_index=0)
        mock_event2.disconnect = AsyncMock()
        queue.active_jobs = [[mock_event, mock_event2]]

        await queue.process_events([mock_event, mock_event2], batch=True)

        queue.call_prediction.assert_called_once()  # called once for both events
        mock_event.disconnect.assert_called_once()

        mock_event2.disconnect.assert_called_once()
        assert queue.clean_event.call_count == 2


class TestGetEventsInBatch:
    def test_empty_event_queue(self, queue: Queue):
        queue.event_queue = deque()
        events, _ = queue.get_events_in_batch()
        assert events is None

    def test_single_type_of_event(self, queue: Queue):
        queue.blocks_dependencies = [{"batch": True, "max_batch_size": 3}]
        queue.event_queue = deque()
        queue.event_queue.extend(
            [
                Event(websocket=MagicMock(), session_hash="test", fn_index=0),
                Event(websocket=MagicMock(), session_hash="test", fn_index=0),
                Event(websocket=MagicMock(), session_hash="test", fn_index=0),
                Event(websocket=MagicMock(), session_hash="test", fn_index=0),
            ]
        )
        events, batch = queue.get_events_in_batch()
        assert batch
        assert [e.fn_index for e in events] == [0, 0, 0]

        events, batch = queue.get_events_in_batch()
        assert batch
        assert [e.fn_index for e in events] == [0]

    def test_multiple_batch_events(self, queue: Queue):
        queue.blocks_dependencies = [
            {"batch": True, "max_batch_size": 3},
            {"batch": True, "max_batch_size": 2},
        ]
        queue.event_queue = deque()
        queue.event_queue.extend(
            [
                Event(websocket=MagicMock(), session_hash="test", fn_index=0),
                Event(websocket=MagicMock(), session_hash="test", fn_index=1),
                Event(websocket=MagicMock(), session_hash="test", fn_index=0),
                Event(websocket=MagicMock(), session_hash="test", fn_index=1),
                Event(websocket=MagicMock(), session_hash="test", fn_index=0),
                Event(websocket=MagicMock(), session_hash="test", fn_index=0),
            ]
        )
        events, batch = queue.get_events_in_batch()
        assert batch
        assert [e.fn_index for e in events] == [0, 0, 0]

        events, batch = queue.get_events_in_batch()
        assert batch
        assert [e.fn_index for e in events] == [1, 1]

        events, batch = queue.get_events_in_batch()
        assert batch
        assert [e.fn_index for e in events] == [0]

    def test_both_types_of_event(self, queue: Queue):
        queue.blocks_dependencies = [
            {"batch": True, "max_batch_size": 3},
            {"batch": False},
        ]
        queue.event_queue = deque()
        queue.event_queue.extend(
            [
                Event(websocket=MagicMock(), session_hash="test", fn_index=0),
                Event(websocket=MagicMock(), session_hash="test", fn_index=1),
                Event(websocket=MagicMock(), session_hash="test", fn_index=0),
                Event(websocket=MagicMock(), session_hash="test", fn_index=1),
                Event(websocket=MagicMock(), session_hash="test", fn_index=1),
            ]
        )
        events, batch = queue.get_events_in_batch()
        assert batch
        assert [e.fn_index for e in events] == [0, 0]

        events, batch = queue.get_events_in_batch()
        assert not (batch)
        assert [e.fn_index for e in events] == [1]
