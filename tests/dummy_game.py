"""Stand-in 'game' for testing the harness end to end: health drops every second,
gold grows, and the health lives right after a unique 8-byte marker so an AOB target works."""
import ctypes
import struct
import time

# marker built at run time so it exists only once in memory (not also as a source literal)
# written byte by byte so the full marker never exists as a separate Python object
buf = ctypes.create_string_buffer(16)
for i, c in enumerate((0x12, 0x0A, 0x17, 0x1B, 0x08, 0x11, 0x7B, 0x7B)):  # marker text XOR 0x5A
    buf[i] = c ^ 0x5A
struct.pack_into("<ii", buf, 8, 100, 50)
addr = ctypes.addressof(buf)
print(f"dummy game running, health at 0x{addr + 8:X}", flush=True)
while True:
    hp, gold = struct.unpack_from("<ii", buf, 8)
    hp = max(0, hp - 1)
    struct.pack_into("<ii", buf, 8, hp, gold + 1)
    print(f"health {hp:4d}   gold {gold:6d}", flush=True)
    time.sleep(1)
