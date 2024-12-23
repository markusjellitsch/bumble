# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# -----------------------------------------------------------------------------
# Imports
# -----------------------------------------------------------------------------
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import logging
import os
import wave
import itertools
from typing import cast, Any, AsyncGenerator, Coroutine, Dict, Optional, Tuple

import click
import pyee

try:
    import lc3  # type: ignore  # pylint: disable=E0401
except ImportError as e:
    raise ImportError("Try `python -m pip install \".[lc3]\"`.") from e

from bumble.colors import color
from bumble import company_ids
from bumble import core
from bumble import gatt
from bumble import hci
from bumble.profiles import bap
from bumble.profiles import le_audio
from bumble.profiles import pbp
from bumble.profiles import bass
import bumble.device
import bumble.transport
import bumble.utils


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------
AURACAST_DEFAULT_DEVICE_NAME = 'Bumble Auracast'
AURACAST_DEFAULT_DEVICE_ADDRESS = hci.Address('F0:F1:F2:F3:F4:F5')
AURACAST_DEFAULT_SYNC_TIMEOUT = 5.0
AURACAST_DEFAULT_ATT_MTU = 256


# -----------------------------------------------------------------------------
# Scan For Broadcasts
# -----------------------------------------------------------------------------
class BroadcastScanner(pyee.EventEmitter):
    @dataclasses.dataclass
    class Broadcast(pyee.EventEmitter):
        name: str | None
        sync: bumble.device.PeriodicAdvertisingSync
        broadcast_id: int
        rssi: int = 0
        public_broadcast_announcement: Optional[pbp.PublicBroadcastAnnouncement] = None
        broadcast_audio_announcement: Optional[bap.BroadcastAudioAnnouncement] = None
        basic_audio_announcement: Optional[bap.BasicAudioAnnouncement] = None
        appearance: Optional[core.Appearance] = None
        biginfo: Optional[bumble.device.BIGInfoAdvertisement] = None
        manufacturer_data: Optional[Tuple[str, bytes]] = None

        def __post_init__(self) -> None:
            super().__init__()
            self.sync.on('establishment', self.on_sync_establishment)
            self.sync.on('loss', self.on_sync_loss)
            self.sync.on('periodic_advertisement', self.on_periodic_advertisement)
            self.sync.on('biginfo_advertisement', self.on_biginfo_advertisement)

        def update(self, advertisement: bumble.device.Advertisement) -> None:
            self.rssi = advertisement.rssi
            for service_data in advertisement.data.get_all(
                core.AdvertisingData.SERVICE_DATA
            ):
                assert isinstance(service_data, tuple)
                service_uuid, data = service_data
                assert isinstance(data, bytes)

                if service_uuid == gatt.GATT_PUBLIC_BROADCAST_ANNOUNCEMENT_SERVICE:
                    self.public_broadcast_announcement = (
                        pbp.PublicBroadcastAnnouncement.from_bytes(data)
                    )
                    continue

                if service_uuid == gatt.GATT_BROADCAST_AUDIO_ANNOUNCEMENT_SERVICE:
                    self.broadcast_audio_announcement = (
                        bap.BroadcastAudioAnnouncement.from_bytes(data)
                    )
                    continue

            self.appearance = advertisement.data.get(  # type: ignore[assignment]
                core.AdvertisingData.APPEARANCE
            )

            if manufacturer_data := advertisement.data.get(
                core.AdvertisingData.MANUFACTURER_SPECIFIC_DATA
            ):
                assert isinstance(manufacturer_data, tuple)
                company_id = cast(int, manufacturer_data[0])
                data = cast(bytes, manufacturer_data[1])
                self.manufacturer_data = (
                    company_ids.COMPANY_IDENTIFIERS.get(
                        company_id, f'0x{company_id:04X}'
                    ),
                    data,
                )

            self.emit('update')

        def print(self) -> None:
            print(
                color('Broadcast:', 'yellow'),
                self.sync.advertiser_address,
                color(self.sync.state.name, 'green'),
            )
            if self.name is not None:
                print(f'  {color("Name", "cyan")}:         {self.name}')
            if self.appearance:
                print(f'  {color("Appearance", "cyan")}:   {str(self.appearance)}')
            print(f'  {color("RSSI", "cyan")}:         {self.rssi}')
            print(f'  {color("SID", "cyan")}:          {self.sync.sid}')

            if self.manufacturer_data:
                print(
                    f'  {color("Manufacturer Data", "cyan")}: '
                    f'{self.manufacturer_data[0]} -> {self.manufacturer_data[1].hex()}'
                )

            if self.broadcast_audio_announcement:
                print(
                    f'  {color("Broadcast ID", "cyan")}: '
                    f'{self.broadcast_audio_announcement.broadcast_id}'
                )

            if self.public_broadcast_announcement:
                print(
                    f'  {color("Features", "cyan")}:     '
                    f'{self.public_broadcast_announcement.features}'
                )
                print(
                    f'  {color("Metadata", "cyan")}:     '
                    f'{self.public_broadcast_announcement.metadata}'
                )

            if self.basic_audio_announcement:
                print(color('  Audio:', 'cyan'))
                print(
                    color('    Presentation Delay:', 'magenta'),
                    self.basic_audio_announcement.presentation_delay,
                )
                for subgroup in self.basic_audio_announcement.subgroups:
                    print(color('    Subgroup:', 'magenta'))
                    print(color('      Codec ID:', 'yellow'))
                    print(
                        color('        Coding Format:           ', 'green'),
                        subgroup.codec_id.codec_id.name,
                    )
                    print(
                        color('        Company ID:              ', 'green'),
                        subgroup.codec_id.company_id,
                    )
                    print(
                        color('        Vendor Specific Codec ID:', 'green'),
                        subgroup.codec_id.vendor_specific_codec_id,
                    )
                    print(
                        color('      Codec Config:', 'yellow'),
                        subgroup.codec_specific_configuration,
                    )
                    print(color('      Metadata:    ', 'yellow'), subgroup.metadata)

                    for bis in subgroup.bis:
                        print(color(f'      BIS [{bis.index}]:', 'yellow'))
                        print(
                            color('       Codec Config:', 'green'),
                            bis.codec_specific_configuration,
                        )

            if self.biginfo:
                print(color('  BIG:', 'cyan'))
                print(
                    color('    Number of BIS:', 'magenta'),
                    self.biginfo.num_bis,
                )
                print(
                    color('    PHY:          ', 'magenta'),
                    self.biginfo.phy.name,
                )
                print(
                    color('    Framed:       ', 'magenta'),
                    self.biginfo.framed,
                )
                print(
                    color('    Encrypted:    ', 'magenta'),
                    self.biginfo.encrypted,
                )

        def on_sync_establishment(self) -> None:
            self.emit('sync_establishment')

        def on_sync_loss(self) -> None:
            self.basic_audio_announcement = None
            self.biginfo = None
            self.emit('sync_loss')

        def on_periodic_advertisement(
            self, advertisement: bumble.device.PeriodicAdvertisement
        ) -> None:
            if advertisement.data is None:
                return

            for service_data in advertisement.data.get_all(
                core.AdvertisingData.SERVICE_DATA
            ):
                assert isinstance(service_data, tuple)
                service_uuid, data = service_data
                assert isinstance(data, bytes)

                if service_uuid == gatt.GATT_BASIC_AUDIO_ANNOUNCEMENT_SERVICE:
                    self.basic_audio_announcement = (
                        bap.BasicAudioAnnouncement.from_bytes(data)
                    )
                    break

            self.emit('change')

        def on_biginfo_advertisement(
            self, advertisement: bumble.device.BIGInfoAdvertisement
        ) -> None:
            self.biginfo = advertisement
            self.emit('change')

    def __init__(
        self,
        device: bumble.device.Device,
        filter_duplicates: bool,
        sync_timeout: float,
    ):
        super().__init__()
        self.device = device
        self.filter_duplicates = filter_duplicates
        self.sync_timeout = sync_timeout
        self.broadcasts = dict[hci.Address, BroadcastScanner.Broadcast]()
        device.on('advertisement', self.on_advertisement)

    async def start(self) -> None:
        await self.device.start_scanning(
            active=False,
            filter_duplicates=False,
        )

    async def stop(self) -> None:
        await self.device.stop_scanning()

    def on_advertisement(self, advertisement: bumble.device.Advertisement) -> None:
        if not (
            ads := advertisement.data.get_all(
                core.AdvertisingData.SERVICE_DATA_16_BIT_UUID
            )
        ) or not (
            broadcast_audio_announcement := next(
                (
                    ad
                    for ad in ads
                    if isinstance(ad, tuple)
                    and ad[0] == gatt.GATT_BROADCAST_AUDIO_ANNOUNCEMENT_SERVICE
                ),
                None,
            )
        ):
            return

        broadcast_name = advertisement.data.get(core.AdvertisingData.BROADCAST_NAME)
        assert isinstance(broadcast_name, str) or broadcast_name is None
        assert isinstance(broadcast_audio_announcement[1], bytes)

        if broadcast := self.broadcasts.get(advertisement.address):
            broadcast.update(advertisement)
            return

        bumble.utils.AsyncRunner.spawn(
            self.on_new_broadcast(
                broadcast_name,
                advertisement,
                bap.BroadcastAudioAnnouncement.from_bytes(
                    broadcast_audio_announcement[1]
                ).broadcast_id,
            )
        )

    async def on_new_broadcast(
        self,
        name: str | None,
        advertisement: bumble.device.Advertisement,
        broadcast_id: int,
    ) -> None:
        periodic_advertising_sync = await self.device.create_periodic_advertising_sync(
            advertiser_address=advertisement.address,
            sid=advertisement.sid,
            sync_timeout=self.sync_timeout,
            filter_duplicates=self.filter_duplicates,
        )
        broadcast = self.Broadcast(name, periodic_advertising_sync, broadcast_id)
        broadcast.update(advertisement)
        self.broadcasts[advertisement.address] = broadcast
        periodic_advertising_sync.on('loss', lambda: self.on_broadcast_loss(broadcast))
        self.emit('new_broadcast', broadcast)

    def on_broadcast_loss(self, broadcast: Broadcast) -> None:
        del self.broadcasts[broadcast.sync.advertiser_address]
        bumble.utils.AsyncRunner.spawn(broadcast.sync.terminate())
        self.emit('broadcast_loss', broadcast)


class PrintingBroadcastScanner(pyee.EventEmitter):
    def __init__(
        self, device: bumble.device.Device, filter_duplicates: bool, sync_timeout: float
    ) -> None:
        super().__init__()
        self.scanner = BroadcastScanner(device, filter_duplicates, sync_timeout)
        self.scanner.on('new_broadcast', self.on_new_broadcast)
        self.scanner.on('broadcast_loss', self.on_broadcast_loss)
        self.scanner.on('update', self.refresh)
        self.status_message = ''

    async def start(self) -> None:
        self.status_message = color('Scanning...', 'green')
        await self.scanner.start()

    def on_new_broadcast(self, broadcast: BroadcastScanner.Broadcast) -> None:
        self.status_message = color(
            f'+Found {len(self.scanner.broadcasts)} broadcasts', 'green'
        )
        broadcast.on('change', self.refresh)
        broadcast.on('update', self.refresh)
        self.refresh()

    def on_broadcast_loss(self, broadcast: BroadcastScanner.Broadcast) -> None:
        self.status_message = color(
            f'-Found {len(self.scanner.broadcasts)} broadcasts', 'green'
        )
        self.refresh()

    def refresh(self) -> None:
        # Clear the screen from the top
        print('\033[H')
        print('\033[0J')
        print('\033[H')

        # Print the status message
        print(self.status_message)
        print("==========================================")

        # Print all broadcasts
        for broadcast in self.scanner.broadcasts.values():
            broadcast.print()
            print('------------------------------------------')

        # Clear the screen to the bottom
        print('\033[0J')


@contextlib.asynccontextmanager
async def create_device(transport: str) -> AsyncGenerator[bumble.device.Device, Any]:
    async with await bumble.transport.open_transport(transport) as (
        hci_source,
        hci_sink,
    ):
        device_config = bumble.device.DeviceConfiguration(
            name=AURACAST_DEFAULT_DEVICE_NAME,
            address=AURACAST_DEFAULT_DEVICE_ADDRESS,
            keystore='JsonKeyStore',
        )

        device = bumble.device.Device.from_config_with_hci(
            device_config,
            hci_source,
            hci_sink,
        )
        await device.power_on()

        yield device


async def find_broadcast_by_name(
    device: bumble.device.Device, name: Optional[str]
) -> BroadcastScanner.Broadcast:
    result = asyncio.get_running_loop().create_future()

    def on_broadcast_change(broadcast: BroadcastScanner.Broadcast) -> None:
        if broadcast.basic_audio_announcement and not result.done():
            print(color('Broadcast basic audio announcement received', 'green'))
            result.set_result(broadcast)

    def on_new_broadcast(broadcast: BroadcastScanner.Broadcast) -> None:
        if name is None or broadcast.name == name:
            print(color('Broadcast found:', 'green'), broadcast.name)
            broadcast.on('change', lambda: on_broadcast_change(broadcast))
            return

        print(color(f'Skipping broadcast {broadcast.name}'))

    scanner = BroadcastScanner(device, False, AURACAST_DEFAULT_SYNC_TIMEOUT)
    scanner.on('new_broadcast', on_new_broadcast)
    await scanner.start()

    broadcast = await result
    await scanner.stop()

    return broadcast


async def run_scan(
    filter_duplicates: bool, sync_timeout: float, transport: str
) -> None:
    async with create_device(transport) as device:
        if not device.supports_le_periodic_advertising:
            print(color('Periodic advertising not supported', 'red'))
            return

        scanner = PrintingBroadcastScanner(device, filter_duplicates, sync_timeout)
        await scanner.start()
        await asyncio.get_running_loop().create_future()


async def run_assist(
    broadcast_name: Optional[str],
    source_id: Optional[int],
    command: str,
    transport: str,
    address: str,
) -> None:
    async with create_device(transport) as device:
        if not device.supports_le_periodic_advertising:
            print(color('Periodic advertising not supported', 'red'))
            return

        # Connect to the server
        print(f'=== Connecting to {address}...')
        connection = await device.connect(address)
        peer = bumble.device.Peer(connection)
        print(f'=== Connected to {peer}')

        print("+++ Encrypting connection...")
        await peer.connection.encrypt()
        print("+++ Connection encrypted")

        # Request a larger MTU
        mtu = AURACAST_DEFAULT_ATT_MTU
        print(color(f'$$$ Requesting MTU={mtu}', 'yellow'))
        await peer.request_mtu(mtu)

        # Get the BASS service
        bass_client = await peer.discover_service_and_create_proxy(
            bass.BroadcastAudioScanServiceProxy
        )

        # Check that the service was found
        if not bass_client:
            print(color('!!! Broadcast Audio Scan Service not found', 'red'))
            return

        # Subscribe to and read the broadcast receive state characteristics
        for i, broadcast_receive_state in enumerate(
            bass_client.broadcast_receive_states
        ):
            try:
                await broadcast_receive_state.subscribe(
                    lambda value, i=i: print(
                        f"{color(f'Broadcast Receive State Update [{i}]:', 'green')} {value}"
                    )
                )
            except core.ProtocolError as error:
                print(
                    color(
                        f'!!! Failed to subscribe to Broadcast Receive State characteristic:',
                        'red',
                    ),
                    error,
                )
            value = await broadcast_receive_state.read_value()
            print(
                f'{color(f"Initial Broadcast Receive State [{i}]:", "green")} {value}'
            )

        if command == 'monitor-state':
            await peer.sustain()
            return

        if command == 'add-source':
            # Find the requested broadcast
            await bass_client.remote_scan_started()
            if broadcast_name:
                print(color('Scanning for broadcast:', 'cyan'), broadcast_name)
            else:
                print(color('Scanning for any broadcast', 'cyan'))
            broadcast = await find_broadcast_by_name(device, broadcast_name)

            if broadcast.broadcast_audio_announcement is None:
                print(color('No broadcast audio announcement found', 'red'))
                return

            if (
                broadcast.basic_audio_announcement is None
                or not broadcast.basic_audio_announcement.subgroups
            ):
                print(color('No subgroups found', 'red'))
                return

            # Add the source
            print(color('Adding source:', 'blue'), broadcast.sync.advertiser_address)
            await bass_client.add_source(
                broadcast.sync.advertiser_address,
                broadcast.sync.sid,
                broadcast.broadcast_audio_announcement.broadcast_id,
                bass.PeriodicAdvertisingSyncParams.SYNCHRONIZE_TO_PA_PAST_AVAILABLE,
                0xFFFF,
                [
                    bass.SubgroupInfo(
                        bass.SubgroupInfo.ANY_BIS,
                        bytes(broadcast.basic_audio_announcement.subgroups[0].metadata),
                    )
                ],
            )

            # Initiate a PA Sync Transfer
            await broadcast.sync.transfer(peer.connection)

            # Notify the sink that we're done scanning.
            await bass_client.remote_scan_stopped()

            await peer.sustain()
            return

        if command == 'modify-source':
            if source_id is None:
                print(color('!!! modify-source requires --source-id'))
                return

            # Find the requested broadcast
            await bass_client.remote_scan_started()
            if broadcast_name:
                print(color('Scanning for broadcast:', 'cyan'), broadcast_name)
            else:
                print(color('Scanning for any broadcast', 'cyan'))
            broadcast = await find_broadcast_by_name(device, broadcast_name)

            if broadcast.broadcast_audio_announcement is None:
                print(color('No broadcast audio announcement found', 'red'))
                return

            if (
                broadcast.basic_audio_announcement is None
                or not broadcast.basic_audio_announcement.subgroups
            ):
                print(color('No subgroups found', 'red'))
                return

            # Modify the source
            print(
                color('Modifying source:', 'blue'),
                source_id,
            )
            await bass_client.modify_source(
                source_id,
                bass.PeriodicAdvertisingSyncParams.SYNCHRONIZE_TO_PA_PAST_NOT_AVAILABLE,
                0xFFFF,
                [
                    bass.SubgroupInfo(
                        bass.SubgroupInfo.ANY_BIS,
                        bytes(broadcast.basic_audio_announcement.subgroups[0].metadata),
                    )
                ],
            )
            await peer.sustain()
            return

        if command == 'remove-source':
            if source_id is None:
                print(color('!!! remove-source requires --source-id'))
                return

            # Remove the source
            print(color('Removing source:', 'blue'), source_id)
            await bass_client.remove_source(source_id)
            await peer.sustain()
            return

        print(color(f'!!! invalid command {command}'))


async def run_pair(transport: str, address: str) -> None:
    async with create_device(transport) as device:

        # Connect to the server
        print(f'=== Connecting to {address}...')
        async with device.connect_as_gatt(address) as peer:
            print(f'=== Connected to {peer}')

            print("+++ Initiating pairing...")
            await peer.connection.pair()
            print("+++ Paired")


async def run_receive(
    transport: str,
    broadcast_id: int,
    broadcast_code: str | None,
    sync_timeout: float,
    subgroup_index: int,
) -> None:
    async with create_device(transport) as device:
        if not device.supports_le_periodic_advertising:
            print(color('Periodic advertising not supported', 'red'))
            return

        scanner = BroadcastScanner(device, False, sync_timeout)
        scan_result: asyncio.Future[BroadcastScanner.Broadcast] = (
            asyncio.get_running_loop().create_future()
        )

        def on_new_broadcast(broadcast: BroadcastScanner.Broadcast) -> None:
            if scan_result.done():
                return
            if broadcast.broadcast_id == broadcast_id:
                scan_result.set_result(broadcast)

        scanner.on('new_broadcast', on_new_broadcast)
        await scanner.start()
        print('Start scanning...')
        broadcast = await scan_result
        print('Advertisement found:')
        broadcast.print()
        basic_audio_announcement_scanned = asyncio.Event()

        def on_change() -> None:
            if (
                broadcast.basic_audio_announcement
                and not basic_audio_announcement_scanned.is_set()
            ):
                basic_audio_announcement_scanned.set()

        broadcast.on('change', on_change)
        if not broadcast.basic_audio_announcement:
            print('Wait for Basic Audio Announcement...')
            await basic_audio_announcement_scanned.wait()
        print('Basic Audio Announcement found')
        broadcast.print()
        print('Stop scanning')
        await scanner.stop()
        print('Start sync to BIG')

        assert broadcast.basic_audio_announcement
        subgroup = broadcast.basic_audio_announcement.subgroups[subgroup_index]
        configuration = subgroup.codec_specific_configuration
        assert configuration
        assert (sampling_frequency := configuration.sampling_frequency)
        assert (frame_duration := configuration.frame_duration)

        big_sync = await device.create_big_sync(
            broadcast.sync,
            bumble.device.BigSyncParameters(
                big_sync_timeout=0x4000,
                bis=[bis.index for bis in subgroup.bis],
                broadcast_code=(
                    bytes.fromhex(broadcast_code) if broadcast_code else None
                ),
            ),
        )
        num_bis = len(big_sync.bis_links)
        decoder = lc3.Decoder(
            frame_duration_us=frame_duration.us,
            sample_rate_hz=sampling_frequency.hz,
            num_channels=num_bis,
        )
        sdus = [b''] * num_bis
        subprocess = await asyncio.create_subprocess_shell(
            f'stdbuf -i0 ffplay -ar {sampling_frequency.hz} -ac {num_bis} -f f32le pipe:0',
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        for i, bis_link in enumerate(big_sync.bis_links):
            print(f'Setup ISO for BIS {bis_link.handle}')

            def sink(index: int, packet: hci.HCI_IsoDataPacket):
                nonlocal sdus
                sdus[index] = packet.iso_sdu_fragment
                if all(sdus) and subprocess.stdin:
                    subprocess.stdin.write(decoder.decode(b''.join(sdus)).tobytes())
                    sdus = [b''] * num_bis

            bis_link.sink = functools.partial(sink, i)
            await device.send_command(
                hci.HCI_LE_Setup_ISO_Data_Path_Command(
                    connection_handle=bis_link.handle,
                    data_path_direction=hci.HCI_LE_Setup_ISO_Data_Path_Command.Direction.CONTROLLER_TO_HOST,
                    data_path_id=0,
                    codec_id=hci.CodingFormat(codec_id=hci.CodecID.TRANSPARENT),
                    controller_delay=0,
                    codec_configuration=b'',
                ),
                check_result=True,
            )

        terminated = asyncio.Event()
        big_sync.on(big_sync.Event.TERMINATION, lambda _: terminated.set())
        await terminated.wait()


async def run_broadcast(
    transport: str, broadcast_id: int, broadcast_code: str | None, wav_file_path: str
) -> None:
    async with create_device(transport) as device:
        if not device.supports_le_periodic_advertising:
            print(color('Periodic advertising not supported', 'red'))
            return

        with wave.open(wav_file_path, 'rb') as wav:
            print('Encoding wav file into lc3...')
            encoder = lc3.Encoder(
                frame_duration_us=10000,
                sample_rate_hz=48000,
                num_channels=2,
                input_sample_rate_hz=wav.getframerate(),
            )
            frames = list[bytes]()
            while pcm := wav.readframes(encoder.get_frame_samples()):
                frames.append(
                    encoder.encode(pcm, num_bytes=200, bit_depth=wav.getsampwidth() * 8)
                )
            del encoder
            print('Encoding complete.')

        basic_audio_announcement = bap.BasicAudioAnnouncement(
            presentation_delay=40000,
            subgroups=[
                bap.BasicAudioAnnouncement.Subgroup(
                    codec_id=hci.CodingFormat(codec_id=hci.CodecID.LC3),
                    codec_specific_configuration=bap.CodecSpecificConfiguration(
                        sampling_frequency=bap.SamplingFrequency.FREQ_48000,
                        frame_duration=bap.FrameDuration.DURATION_10000_US,
                        octets_per_codec_frame=100,
                    ),
                    metadata=le_audio.Metadata(
                        [
                            le_audio.Metadata.Entry(
                                tag=le_audio.Metadata.Tag.LANGUAGE, data=b'eng'
                            ),
                            le_audio.Metadata.Entry(
                                tag=le_audio.Metadata.Tag.PROGRAM_INFO, data=b'Disco'
                            ),
                        ]
                    ),
                    bis=[
                        bap.BasicAudioAnnouncement.BIS(
                            index=1,
                            codec_specific_configuration=bap.CodecSpecificConfiguration(
                                audio_channel_allocation=bap.AudioLocation.FRONT_LEFT
                            ),
                        ),
                        bap.BasicAudioAnnouncement.BIS(
                            index=2,
                            codec_specific_configuration=bap.CodecSpecificConfiguration(
                                audio_channel_allocation=bap.AudioLocation.FRONT_RIGHT
                            ),
                        ),
                    ],
                )
            ],
        )
        broadcast_audio_announcement = bap.BroadcastAudioAnnouncement(broadcast_id)
        print('Start Advertising')
        advertising_set = await device.create_advertising_set(
            advertising_parameters=bumble.device.AdvertisingParameters(
                advertising_event_properties=bumble.device.AdvertisingEventProperties(
                    is_connectable=False
                ),
                primary_advertising_interval_min=100,
                primary_advertising_interval_max=200,
            ),
            advertising_data=(
                broadcast_audio_announcement.get_advertising_data()
                + bytes(
                    core.AdvertisingData(
                        [(core.AdvertisingData.BROADCAST_NAME, b'Bumble Auracast')]
                    )
                )
            ),
            periodic_advertising_parameters=bumble.device.PeriodicAdvertisingParameters(
                periodic_advertising_interval_min=80,
                periodic_advertising_interval_max=160,
            ),
            periodic_advertising_data=basic_audio_announcement.get_advertising_data(),
            auto_restart=True,
            auto_start=True,
        )
        print('Start Periodic Advertising')
        await advertising_set.start_periodic()
        print('Setup BIG')
        big = await device.create_big(
            advertising_set,
            parameters=bumble.device.BigParameters(
                num_bis=2,
                sdu_interval=10000,
                max_sdu=100,
                max_transport_latency=65,
                rtn=4,
                broadcast_code=(
                    bytes.fromhex(broadcast_code) if broadcast_code else None
                ),
            ),
        )
        print('Setup ISO Data Path')
        for bis_link in big.bis_links:
            await device.send_command(
                hci.HCI_LE_Setup_ISO_Data_Path_Command(
                    connection_handle=bis_link.handle,
                    data_path_direction=hci.HCI_LE_Setup_ISO_Data_Path_Command.Direction.HOST_TO_CONTROLLER,
                    data_path_id=0,
                    codec_id=hci.CodingFormat(hci.CodecID.TRANSPARENT),
                    controller_delay=0,
                    codec_configuration=b'',
                ),
                check_result=True,
            )

        for frame in itertools.cycle(frames):
            mid = len(frame) // 2
            big.bis_links[0].write(frame[:mid])
            big.bis_links[1].write(frame[mid:])
            await asyncio.sleep(0.009)


def run_async(async_command: Coroutine) -> None:
    try:
        asyncio.run(async_command)
    except core.ProtocolError as error:
        if error.error_namespace == 'att' and error.error_code in list(
            bass.ApplicationError
        ):
            message = bass.ApplicationError(error.error_code).name
        else:
            message = str(error)

        print(
            color('!!! An error occurred while executing the command:', 'red'), message
        )


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
@click.group()
@click.pass_context
def auracast(ctx):
    ctx.ensure_object(dict)


@auracast.command('scan')
@click.option(
    '--filter-duplicates', is_flag=True, default=False, help='Filter duplicates'
)
@click.option(
    '--sync-timeout',
    metavar='SYNC_TIMEOUT',
    type=float,
    default=AURACAST_DEFAULT_SYNC_TIMEOUT,
    help='Sync timeout (in seconds)',
)
@click.argument('transport')
@click.pass_context
def scan(ctx, filter_duplicates, sync_timeout, transport):
    """Scan for public broadcasts"""
    run_async(run_scan(filter_duplicates, sync_timeout, transport))


@auracast.command('assist')
@click.option(
    '--broadcast-name',
    metavar='BROADCAST_NAME',
    help='Broadcast Name to tune to',
)
@click.option(
    '--source-id',
    metavar='SOURCE_ID',
    type=int,
    help='Source ID (for remove-source command)',
)
@click.option(
    '--command',
    type=click.Choice(
        ['monitor-state', 'add-source', 'modify-source', 'remove-source']
    ),
    required=True,
)
@click.argument('transport')
@click.argument('address')
@click.pass_context
def assist(ctx, broadcast_name, source_id, command, transport, address):
    """Scan for broadcasts on behalf of a audio server"""
    run_async(run_assist(broadcast_name, source_id, command, transport, address))


@auracast.command('pair')
@click.argument('transport')
@click.argument('address')
@click.pass_context
def pair(ctx, transport, address):
    """Pair with an audio server"""
    run_async(run_pair(transport, address))


@auracast.command('receive')
@click.argument('transport')
@click.argument('broadcast_id', type=int)
@click.option(
    '--broadcast-code',
    metavar='BROADCAST_CODE',
    type=str,
    help='Broadcast encryption code in hex format',
)
@click.option(
    '--sync-timeout',
    metavar='SYNC_TIMEOUT',
    type=float,
    default=AURACAST_DEFAULT_SYNC_TIMEOUT,
    help='Sync timeout (in seconds)',
)
@click.option(
    '--subgroup',
    metavar='SUBGROUP',
    type=int,
    default=0,
    help='Index of Subgroup',
)
@click.pass_context
def receive(ctx, transport, broadcast_id, broadcast_code, sync_timeout, subgroup):
    """Receive a broadcast source"""
    run_async(
        run_receive(transport, broadcast_id, broadcast_code, sync_timeout, subgroup)
    )


@auracast.command('broadcast')
@click.argument('transport')
@click.argument('wav_file_path', type=str)
@click.option(
    '--broadcast-id',
    metavar='BROADCAST_ID',
    type=int,
    default=123456,
    help='Broadcast ID',
)
@click.option(
    '--broadcast-code',
    metavar='BROADCAST_CODE',
    type=str,
    help='Broadcast encryption code in hex format',
)
@click.pass_context
def broadcast(ctx, transport, broadcast_id, broadcast_code, wav_file_path):
    """Start a broadcast as a source."""
    run_async(
        run_broadcast(
            transport=transport,
            broadcast_id=broadcast_id,
            broadcast_code=broadcast_code,
            wav_file_path=wav_file_path,
        )
    )


def main():
    logging.basicConfig(level=os.environ.get('BUMBLE_LOGLEVEL', 'INFO').upper())
    auracast()


# -----------------------------------------------------------------------------
if __name__ == "__main__":
    main()  # pylint: disable=no-value-for-parameter
