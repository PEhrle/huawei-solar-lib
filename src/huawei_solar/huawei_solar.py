"""
Get production and status information from the Huawei Inverter using Modbus over TCP
"""
import asyncio
import logging
import typing as t
from collections import namedtuple

import backoff
from pymodbus.client.asynchronous.async_io import (
    ReconnectingAsyncioModbusTcpClient,
    init_tcp_client,
)
from pymodbus.constants import Endian
from pymodbus.exceptions import ConnectionException as ModbusConnectionException
from pymodbus.payload import BinaryPayloadDecoder
from pymodbus.pdu import ExceptionResponse

import huawei_solar.register_names as rn

from .exceptions import ConnectionException, ReadException
from .registers import REGISTERS, RegisterDefinition

LOGGER = logging.getLogger(__name__)

Result = namedtuple("Result", "value unit")

DEFAULT_SLAVE = 0
DEFAULT_TIMEOUT = 5
DEFAULT_WAIT = 2
DEFAULT_COOLDOWN_TIME = 0.01


class AsyncHuaweiSolar:
    """Async interface to the Huawei solar inverter"""

    def __init__(
        self,
        client: ReconnectingAsyncioModbusTcpClient,
        slave: int = DEFAULT_SLAVE,
        timeout: int = DEFAULT_TIMEOUT,
        cooldown_time: int = DEFAULT_COOLDOWN_TIME,
    ):
        """DO NOT USE THIS CONSTRUCTOR DIRECTLY. Use AsyncHuaweiSolar.create() instead"""
        self._client = client
        self._timeout = timeout
        self._cooldown_time = cooldown_time
        self.slave = slave

        # use this lock to prevent concurrent requests, as the
        # Huawei inverters can't cope with those
        self._communication_lock = asyncio.Lock()

        # These values are set by the `create()` method
        self.time_zone = None
        self.battery_type = None

    @classmethod
    async def create(
        cls,
        host,
        port="502",
        slave: int = DEFAULT_SLAVE,
        timeout: int = DEFAULT_TIMEOUT,
        cooldown_time: int = DEFAULT_COOLDOWN_TIME,
        loop=None,
    ):  # pylint: disable=too-many-arguments
        """Creates an AsyncHuaweiSolar instance."""

        client = None
        try:
            client = await cls.__get_client(host, port, loop)

            huawei_solar = cls(client, slave, timeout, cooldown_time)

            # get some registers which are needed to correctly decode all values

            huawei_solar.time_zone = (await huawei_solar.get(rn.TIME_ZONE)).value
            # we assume that when at least one battery is present, it will always be put in storage_unit_1 first
            huawei_solar.battery_type = (
                await huawei_solar.get(rn.STORAGE_UNIT_1_PRODUCT_MODEL)
            ).value

            return huawei_solar
        except Exception as err:
            # if an error occurs, we need to make sure that the Modbus-client is stopped,
            # otherwise it can stay active and cause even more problems ...
            if client is not None:
                await client.stop()
            raise err

    @classmethod
    async def __get_client(cls, host, port, loop) -> ReconnectingAsyncioModbusTcpClient:
        client = await init_tcp_client(None, loop, host, port, reset_socket=False)
        # wait a little bit to prevent a timeout on the first request
        await asyncio.sleep(1)

        return client

    async def stop(self):
        """Stop the modbus client."""
        self._client.stop()

    async def _decode_response(
        self, reg: RegisterDefinition, decoder: BinaryPayloadDecoder
    ):
        """Decodes a modbus register and puts it into a Result object."""
        result = reg.decode(decoder, self)

        if not hasattr(reg, "unit") or callable(reg.unit) or isinstance(reg.unit, dict):
            return Result(result, None)
        return Result(result, reg.unit)

    async def get(self, name, slave=None):
        """get named register from device"""
        return (await self.get_multiple([name], slave))[0]

    async def get_multiple(self, names: list[str], slave=None):
        """Read multiple registers at the same time.

        This is only possible if the registers are consecutively available in the
        inverters' memory.
        """

        if len(names) == 0:
            raise ValueError("Expected at least one register name")

        registers = list(map(REGISTERS.get, names))

        if None in registers:
            raise ValueError("Did not recognize all register names")

        for idx in range(1, len(names)):
            if (
                registers[idx - 1].register + registers[idx - 1].length
                > registers[idx].register
            ):
                raise ValueError(
                    f"Requested registers must be in monotonically increasing order, "
                    f"but {registers[idx-1].register} + {registers[idx-1].length} > {registers[idx].register}!"
                )

            register_distance = (
                registers[idx - 1].register
                + registers[idx - 1].length
                - registers[idx].register
            )

            if register_distance > 64:
                raise ValueError(
                    "Gap between requested registers is too large. Split it in two requests"
                )

        total_length = (
            registers[-1].register + registers[-1].length - registers[0].register
        )

        response = await self._read_registers(
            registers[0].register, total_length, slave
        )

        if isinstance(response, ExceptionResponse):
            raise ReadException(
                f"Got error while reading from register {registers[0].register} with length {total_length}: {response}"
            )

        decoder = BinaryPayloadDecoder.fromRegisters(
            response.registers, byteorder=Endian.Big, wordorder=Endian.Big
        )

        result = [await self._decode_response(registers[0], decoder)]
        for idx in range(1, len(registers)):
            skip_registers = registers[idx].register - (
                registers[idx - 1].register + registers[idx - 1].length
            )
            decoder.skip_bytes(
                skip_registers * 2
            )  # registers are 16-bit, so we need to multiply by two
            result.append(await self._decode_response(registers[idx], decoder))

        return result

    async def _read_registers(
        self, register: RegisterDefinition, length: int, slave: t.Optional[int]
    ):
        """
        Async read register from device.

        The device needs a bit of time between the connection and the first request
        and between requests if there is a long time between them, else it will fail.

        This is solved by sleeping between the first connection and a request,
        and up to 5 retries between following requests.

        It seems to only support connections from one device at the same time.
        """

        def backoff_giveup(details):
            raise ReadException(
                f"Failed to read register {register} after {details['tries']} tries"
            )

        @backoff.on_exception(
            backoff.constant,
            (asyncio.TimeoutError),
            interval=DEFAULT_WAIT,
            max_tries=5,
            jitter=None,
            on_backoff=lambda details: LOGGER.debug(
                "Backing off reading for %0.1f seconds after %d tries",
                details["wait"],
                details["tries"],
            ),
            on_giveup=backoff_giveup,
        )
        async def _do_read():

            if not self._client.connected:
                message = "Modbus client is not connected to the inverter."
                LOGGER.exception(message)
                raise ConnectionException(message)
            try:
                response = await self._client.protocol.read_holding_registers(
                    register,
                    length,
                    unit=slave or self.slave,
                    timeout=self._timeout,
                )
                return response

            except ModbusConnectionException as err:
                message = (
                    "could not read register value, "
                    "is an other device already connected?"
                )
                LOGGER.error(message)
                raise ReadException(message) from err
            # errors are different with async pymodbus,
            # we should not be able to reach this code. Keep it for debugging
            message = "could not read register value for unknown reason"
            LOGGER.error(message)
            raise ReadException(message)

        async with self._communication_lock:
            LOGGER.debug("Reading register %s", register)
            result = await _do_read()
            await asyncio.sleep(
                self._cooldown_time
            )  # throttle requests to prevent errors
            return result
