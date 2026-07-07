import unittest
import json
from importlib.resources import files
from automower_ble.protocol import Command, MowerState, MowerActivity, BLEClient
from automower_ble.models import MowerModels


with files("automower_ble").joinpath("protocol.json").open("r") as _f:
    _PROTOCOL = json.load(_f)


class TestRequestMethods(unittest.TestCase):
    def setUp(self):
        with files("automower_ble").joinpath("protocol.json").open("r") as f:
            self.protocol = json.load(f)  # Load the parameters to have them available

    def test_decode_response_device_type(self):
        command = Command(1197489078, parameter=self.protocol["GetModel"])

        response = command.parse_response(
            bytearray.fromhex("02fd1300b63b604701e601af5a1209000002001701c803")
        )
        self.assertEqual(
            MowerModels[(response["deviceType"], response["deviceVariant"])].model,
            "Automower 305",
        )

        response = command.parse_response(
            bytearray.fromhex("02fd130038e38f0b01dc01af5a1209000002000c005903")
        )
        self.assertEqual(
            MowerModels[(response["deviceType"], response["deviceVariant"])].model,
            "Automower 315",
        )

    def test_decode_response_is_charging(self):
        command = Command(1197489078, self.protocol["IsCharging"])

        self.assertEqual(
            command.parse_response(
                bytearray.fromhex("02fd1200b63b604701db01af0a101500000100011603")
            )["response"],
            True,
        )
        self.assertEqual(
            command.parse_response(
                bytearray.fromhex("02fd1200b63b604701db01af0a101500000100004803")
            )["response"],
            False,
        )

    def test_decode_response_mower_state(self):
        command = Command(1197489078, self.protocol["GetState"])

        self.assertNotIn(
            command.parse_response(
                bytearray.fromhex("02fd1200b33b6047010901afea110100000100008103")
            )["response"],
            [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 13, 14],  # 11=unknown
        )

        command = Command(876143061, self.protocol["GetState"])
        self.assertEqual(
            command.parse_response(
                bytearray.fromhex("02fd1200d5e13834012301afea110200000100033a03")
            )["response"],
            MowerState.FATAL_ERROR.value,
        )

    def test_decode_response_mower_activity(self):
        response = Command(1197489078, self.protocol["GetActivity"])

        self.assertEqual(
            response.parse_response(
                bytearray.fromhex("02fd1200b33b6047010901afea110200000100026403")
            )["response"],
            MowerActivity.GOING_OUT.value,  # 2 = goingOut
        )

    def test_decode_get_task_response(self):
        response = Command(1197489078, self.protocol["GetTask"])
        decoded = response.parse_response(
            bytearray.fromhex(
                "02fd240025be246a010701af5212050000130000e1000038310000010001010001013003"
            )
        )

        self.assertEqual(
            decoded["start"],
            57600,
        )
        self.assertEqual(
            decoded["duration"],
            12600,
        )
        self.assertEqual(decoded["useOnMonday"], 1)
        self.assertEqual(decoded["useOnTuesday"], 0)
        self.assertEqual(decoded["useOnWednesday"], 1)
        self.assertEqual(decoded["useOnThursday"], 1)
        self.assertEqual(decoded["useOnFriday"], 0)
        self.assertEqual(decoded["useOnSaturday"], 1)
        self.assertEqual(decoded["useOnSunday"], 1)

    def test_decode_get_number_of_tasks_response(self):
        response = Command(0x13A51453, self.protocol["GetNumberOfTasks"])
        self.assertEqual(
            response.parse_response(
                bytearray.fromhex("02fd150025be246a012e01af52120400000400010000004f03")
            )["response"],
            1,
        )

    def test_is_matching_response_accepts_real_reply(self):
        # The genuine EnterOperatorPin response seen on a healthy reconnect.
        command = Command(1197489078, self.protocol["EnterOperatorPin"])
        self.assertTrue(
            command.is_matching_response(
                bytearray.fromhex("02fd1100b63b6047019c01af38120400000000ab03")
            )
        )

    def test_is_matching_response_rejects_spurious_frame(self):
        # Short "busy"-style frame the mower emits while still moving right
        # after a reconnect. It is well-framed but is not a reply to our
        # command and previously crashed connect() with an index error.
        command = Command(1197489078, self.protocol["EnterOperatorPin"])
        self.assertFalse(
            command.is_matching_response(
                bytearray.fromhex("02fd0b00b63b604700b20901011403")
            )
        )

    def test_parse_response_short_frame_returns_none(self):
        # parse_response must not raise on a truncated/spurious frame.
        command = Command(1197489078, self.protocol["EnterOperatorPin"])
        self.assertIsNone(
            command.parse_response(
                bytearray.fromhex("02fd0b00b63b604700b20901011403")
            )
        )

    def test_get_response_result_short_frame_no_crash(self):
        from automower_ble.protocol import BLEClient, ResponseResult

        client = BLEClient(1197489078, "00:00:00:00:00:00")
        # Must return a value (UNKNOWN_ERROR) instead of raising IndexError.
        self.assertEqual(
            client.get_response_result(
                bytearray.fromhex("02fd0b00b63b604700b20901011403")
            ),
            ResponseResult.UNKNOWN_ERROR,
        )


# ── Streaming frame reassembly (BLEClient._read_frame) ─────────────────────────
# These reproduce the notification patterns seen on a real reconnect where the
# mower floods duplicate frames and delivers the trailing 0x03 terminator in a
# separate notification.

def _new_client() -> BLEClient:
    return BLEClient(1197489078, "00:00:00:00:00:00")


async def _feed(client: BLEClient, *chunks_hex: str):
    for hx in chunks_hex:
        await client.queue.put(bytearray.fromhex(hx))


async def test_read_frame_split_terminator():
    # Frame body arrives without its 0x03 terminator, which comes separately.
    client = _new_client()
    await _feed(
        client,
        "02fd1100b63b6047019c01af38120400000000ab",  # 20 bytes, no terminator
        "03",                                          # terminator alone
    )
    frame = await client._read_frame()
    assert frame == bytearray.fromhex(
        "02fd1100b63b6047019c01af38120400000000ab03"
    )


async def test_read_frame_duplicate_flood_before_terminator():
    # The exact pattern from the field log: body, a duplicate body, then the
    # terminator (also duplicated). Must still yield exactly one clean frame.
    client = _new_client()
    await _feed(
        client,
        "02fd1100b63b6047019c01af38120400000000ab",  # body
        "02fd1100b63b6047019c01af38120400000000ab",  # duplicate body
        "03",                                          # terminator
        "03",                                          # duplicate terminator
    )
    frame = await client._read_frame()
    assert frame == bytearray.fromhex(
        "02fd1100b63b6047019c01af38120400000000ab03"
    )


async def test_read_frame_skips_leading_bare_terminators():
    # Leftover bare 0x03 fragments from a previous flood must not abort the
    # read; the parser keeps waiting for the real frame.
    client = _new_client()
    await _feed(
        client,
        "03",
        "03",
        "02fd0d0000000000006315b63b6047b603",  # a complete short frame
    )
    frame = await client._read_frame()
    assert frame == bytearray.fromhex("02fd0d0000000000006315b63b6047b603")


async def test_read_data_skips_unmatched_then_returns_real():
    # _read_data must skip a well-framed but non-matching frame and return the
    # genuine EnterOperatorPin reply that follows.
    client = _new_client()
    cmd = Command(1197489078, _PROTOCOL["EnterOperatorPin"])
    await _feed(
        client,
        "02fd0b00b63b604700b20901011403",            # spurious busy frame
        "02fd1100b63b6047019c01af38120400000000ab",  # real reply body
        "03",                                          # terminator
    )
    frame = await client._read_data(cmd.is_matching_response)
    assert frame == bytearray.fromhex(
        "02fd1100b63b6047019c01af38120400000000ab03"
    )


async def test_read_frame_triplicated_multifragment():
    # Field reproduction: the firmware repeats every notification 3× AND a long
    # frame is split across several notifications. The 42-byte GetSerialNumber
    # response = A(20) + B(20) + C(2), each chunk delivered three times.
    client = _new_client()
    await _feed(
        client,
        "02fd2600b63b6047013b01af5a12050000150050",  # A ×3
        "02fd2600b63b6047013b01af5a12050000150050",
        "02fd2600b63b6047013b01af5a12050000150050",
        "3132000000000000000000000000000000000000",  # B ×3
        "3132000000000000000000000000000000000000",
        "3132000000000000000000000000000000000000",
        "3503",                                        # C (CRC + terminator) ×3
        "3503",
        "3503",
    )
    frame = await client._read_frame()
    assert frame == bytearray.fromhex(
        "02fd2600b63b6047013b01af5a120500001500503132"
        "0000000000000000000000000000000000003503"
    )


async def test_read_frame_triplicated_short_frame():
    # Each notification arrives 3×; a short single-notification frame plus a
    # separately-delivered terminator must still assemble cleanly.
    client = _new_client()
    await _feed(
        client,
        "02fd1200b63b604701db01afea11020000010006",  # body ×3
        "02fd1200b63b604701db01afea11020000010006",
        "02fd1200b63b604701db01afea11020000010006",
        "0503",                                        # CRC + terminator ×3
        "0503",
        "0503",
    )
    frame = await client._read_frame()
    assert frame == bytearray.fromhex(
        "02fd1200b63b604701db01afea110200000100060503"
    )


if __name__ == "__main__":
    unittest.main()
