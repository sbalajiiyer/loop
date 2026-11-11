from loop.ble import protocol as P
from loop.ble.frames import FrameAssembler, build_frame, crc16_modbus, crc32, parse_standard_hr


def test_check_vectors():
    assert crc16_modbus(b"123456789") == 0x4B37
    assert crc32(b"123456789") == 0xCBF43926


def test_client_hello_rebuilds_byte_for_byte():
    # type 35 command frame, seq 1, command 0x91, data 0x01
    assert build_frame(bytes([0x23, 0x01, 0x91, 0x01])) == P.CLIENT_HELLO


def test_assembler_handles_fragments_and_noise():
    a = build_frame(b"\x28" + bytes(range(23)))
    b = build_frame(b"\x31\x05\x02" + bytes(12))
    stream = b"\x00\x13" + a + b"\xaa\x01\xff" + b
    asm, out = FrameAssembler(), []
    for i in range(0, len(stream), 7):          # 7-byte "MTU" fragments
        out += asm.feed(stream[i:i + 7])
    assert [f.packet_type for f in out] == [0x28, 0x31]


def test_standard_hr():
    bpm, rr = parse_standard_hr(bytes([0x10, 62, 0x00, 0x04]))  # 1024/1024 s
    assert bpm == 62 and rr == [1000.0]
