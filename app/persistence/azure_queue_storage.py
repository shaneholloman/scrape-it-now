from typing import Any, AsyncGenerator

from azure.core.exceptions import (
    ResourceExistsError,
    ResourceNotFoundError,
    ServiceRequestError,
)
from azure.storage.queue.aio import QueueClient, QueueServiceClient
from pydantic import BaseModel
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from app.helpers.logging import logger
from app.models.message import Message
from app.persistence.iqueue import IQueue, MessageNotFoundError


class Config(BaseModel):
    connection_string: str
    name: str


class AzureQueueStorage(IQueue):
    _client: QueueClient
    _config: Config
    _service: QueueServiceClient

    def __init__(
        self,
        config: Config,
    ) -> None:
        logger.info('Azure Queue Storage "%s" is configured', config.name)
        self._config = config

    @retry(
        reraise=True,
        retry=retry_if_exception_type(ServiceRequestError),  # Catch for network errors
        stop=stop_after_attempt(8),
        wait=wait_random_exponential(multiplier=0.8, max=60),
    )
    async def send_message(
        self,
        message: str,
    ) -> None:
        await self._client.send_message(message)

    @retry(
        reraise=True,
        retry=retry_if_exception_type(ServiceRequestError),  # Catch for network errors
        stop=stop_after_attempt(8),
        wait=wait_random_exponential(multiplier=0.8, max=60),
    )
    async def receive_messages(
        self,
        max_messages: int,
        visibility_timeout: int,
    ) -> AsyncGenerator[Message, None]:
        messages = self._client.receive_messages(
            max_messages=max_messages,
            visibility_timeout=visibility_timeout,
        )
        async for message in messages:
            yield Message(
                content=message.content,
                delete_token=message.pop_receipt,
                dequeue_count=message.dequeue_count,
                message_id=message.id,
                visibility_timeout=message.next_visible_on,
            )

    async def delete_message(
        self,
        message: Message,
    ) -> None:
        try:
            await self._client.delete_message(
                message=message.message_id,
                pop_receipt=message.delete_token,
            )
        except ResourceNotFoundError as e:
            raise MessageNotFoundError(
                f'Message "{message.message_id}" not found'
            ) from e

    async def __aenter__(self) -> "AzureQueueStorage":
        self._service = QueueServiceClient.from_connection_string(
            self._config.connection_string
        )
        self._client = self._service.get_queue_client(self._config.name)
        # Create if it does not exist
        try:
            await self._client.create_queue()
            logger.info('Created Queue Storage "%s"', self._config.name)
        except ResourceExistsError:
            pass
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._service.close()