from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceNotFoundError,
    ServiceRequestError,
)
from azure.storage.blob.aio import BlobServiceClient, ContainerClient
from pydantic import BaseModel
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from app.helpers.logging import logger
from app.persistence.iblob import (
    BlobAlreadyExistsError,
    BlobNotFoundError,
    IBlob,
    LeaseAlreadyExistsError,
    LeaseNotFoundError,
)


class Config(BaseModel):
    connection_string: str
    name: str


class AzureBlobStorage(IBlob):
    _client: ContainerClient
    _config: Config
    _service: BlobServiceClient

    def __init__(
        self,
        config: Config,
    ) -> None:
        logger.info('Azure Blob Storage "%s" is configured', config.name)
        self._config = config

    @asynccontextmanager
    async def lease_blob(
        self,
        blob: str,
        lease_duration: int,
    ) -> AsyncGenerator[str, None]:
        try:
            # Create the lease
            async with await self._client.get_blob_client(blob).acquire_lease(
                lease_duration
            ) as lease:
                # Return the lease ID
                yield lease.id
        except ResourceExistsError as e:
            raise LeaseAlreadyExistsError(
                f'Lease for blob "{blob}" already exists'
            ) from e
        except ResourceNotFoundError as e:
            raise BlobNotFoundError(f'Blob "{blob}" not found') from e
        except HttpResponseError as e:
            if (
                "There is currently a lease on the blob and no lease ID was specified in the request."
                in e.message
            ):
                raise LeaseAlreadyExistsError(
                    "Lease ID is required to overwrite a blob with an existing lease"
                ) from e
            raise e

    @retry(
        reraise=True,
        retry=retry_if_exception_type(ServiceRequestError),  # Catch for network errors
        stop=stop_after_attempt(8),
        wait=wait_random_exponential(multiplier=0.8, max=60),
    )
    async def upload_blob(
        self,
        blob: str,
        data: bytes,
        length: int,
        overwrite: bool,
        lease_id: str | None = None,
    ) -> None:
        try:
            await self._client.upload_blob(
                data=data,
                encoding=self.encoding,
                lease=lease_id,
                length=length,
                name=blob,
                overwrite=overwrite,
            )
        except ResourceExistsError as e:
            raise BlobAlreadyExistsError(f'Blob "{blob}" already exists') from e
        except HttpResponseError as e:
            if (
                "There is currently a lease on the blob and no lease ID was specified in the request."
                in e.message
            ):
                raise LeaseAlreadyExistsError(
                    "Lease ID is required to overwrite a blob with an existing lease"
                ) from e
            if "There is currently no lease on the blob." in e.message:
                raise LeaseNotFoundError(f'Lease for blob "{blob}" not found') from e
            if (
                "The lease ID specified did not match the lease ID for the blob."
                in e.message
            ):
                raise LeaseAlreadyExistsError(
                    "Provided lease ID does not match the existing"
                ) from e
            if (
                "A lease ID was specified, but the lease for the blob has expired."
                in e.message
            ):
                raise LeaseNotFoundError(f'Lease for blob "{blob}" not found') from e
            raise e

    @retry(
        reraise=True,
        retry=retry_if_exception_type(ServiceRequestError),  # Catch for network errors
        stop=stop_after_attempt(8),
        wait=wait_random_exponential(multiplier=0.8, max=60),
    )
    async def download_blob(
        self,
        blob: str,
    ) -> str:
        try:
            f = await self._client.download_blob(
                blob=blob,
                encoding=self.encoding,
            )
            return await f.readall()
        except ResourceNotFoundError as e:
            raise BlobNotFoundError(f'Blob "{blob}" not found') from e
        except HttpResponseError as e:
            if (
                "The requested URI does not represent any resource on the server."
                in e.message
            ):
                raise BlobNotFoundError(f'Blob "{blob}" not found') from e
            raise e

    @retry(
        reraise=True,
        retry=retry_if_exception_type(ServiceRequestError),  # Catch for network errors
        stop=stop_after_attempt(8),
        wait=wait_random_exponential(multiplier=0.8, max=60),
    )
    async def delete_container(
        self,
    ) -> None:
        await self._client.delete_container()

    async def __aenter__(self) -> "AzureBlobStorage":
        self._service = BlobServiceClient.from_connection_string(
            self._config.connection_string
        )
        self._client = self._service.get_container_client(self._config.name)
        # Create if it does not exist
        try:
            await self._client.create_container()
            logger.info('Created Blob Storage "%s"', self._config.name)
        except ResourceExistsError:
            pass
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._service.close()
