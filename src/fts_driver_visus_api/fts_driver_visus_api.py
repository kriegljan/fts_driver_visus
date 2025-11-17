#!/usr/bin/env python
import socket
import struct
import threading
import time
import rospy

# Error codes mapping
ERROR_CODES = {
    0: "None",
    1: "Unknown Command",
    2: "Invalid Command Length",
    3: "Invalid Command Value",
    4: "Busy",
    5: "Streaming Active",
    6: "Storage Error",
    7: "Internal Bus Error",
    8: "Timeout",
    16: "User Level Not Sufficient",
    17: "Is Read Only",
    18: "Is Write Only",
    19: "Index Does Not Exist",
    20: "Subindex Does Not Exist",
    21: "Invalid Parameter Value Length",
    22: "Invalid Parameter Value",
    25: "Parameters Are Locked",
}

# Packet header constants
SYNC_BYTES = b'\xFF\xFF'
HEADER_LEN = 2 + 2 + 2  # sync(2) + packet_counter(2) + length(2) == 6 bytes
PROCESS_PACKET_ID = 0x01

UDP_PORT=54843
UDP_MESSAGE_SIZE=35

# Timeouts defaults
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_RW_TIMEOUT = 5.0

class SensorProtocolError(Exception):
    """Protocol level parsing/validation error."""
    pass

class SensorConnectionError(Exception):
    """Socket/connect related error."""
    pass
class SensorConnectionTimeout(Exception):
    """Socket/connect timeout."""
    pass
class SensorAPIError(Exception):
    """Sensor API error"""
    pass
class SensorRuntimeError(Exception):
    """Sensor runtime error"""
    pass

class SchunkFmsDriver:
    """
    Driver for Schunk force-torque sensor over TCP.

    Packet layout (on-the-wire):
      Header (6 bytes):
        0-1: sync 0xFF 0xFF
        2-3: packet counter (uint16 little-endian)
        4-5: length N (uint16 little-endian) of user payload
      Payload (N bytes): user/application data, starts at payload[0]
    """

    def __init__(self, connect_timeout=DEFAULT_CONNECT_TIMEOUT, rw_timeout=DEFAULT_RW_TIMEOUT):
        rospy.logdebug("Initializing SchunkFmsDriver")
        self.connect_timeout = float(connect_timeout)
        self.rw_timeout = float(rw_timeout)

        self.sock = None
        self._listener_thread = None
        self._listener_stop = threading.Event()

        self._latest_process = None

        self._pending_lock = threading.Lock() # protects _pending_responses dict

        self._expected_seq = None             # for packet loss detection on incoming packets
        self._send_seq = 0                    # internal outgoing packet counter (0..65535)

        # UDP stream state
        self._udp_thread = None
        self._udp_socket = None
        self._udp_running = False

        # pending responses: seq -> {'cond': threading.Condition(), 'response': bytes or None}
        self._pending_responses = {}

    def connect(self, ip, port=82):
        """
        Open a TCP connection to the sensor and start the listener thread.
        Raises SensorConnectionError on failure.
        """
        rospy.loginfo("Connecting to sensor at %s:%d", ip, port)
        self.ip = ip
        if self.sock:
            raise SensorConnectionError("Already connected")
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self.connect_timeout)
        try:
            s.connect((ip, port))
        except Exception as e:
            s.close()
            rospy.logerr("Connect failed: %s", str(e))
            raise SensorConnectionError(str(e))
        s.settimeout(self.rw_timeout)
        self.sock = s
        self._listener_stop.clear()
        self._listener_thread = threading.Thread(target=self._listener_loop, daemon=True)
        self._listener_thread.start()
        rospy.loginfo("Connected and listener started")

    def disconnect(self):
        """
        Close the TCP connection and stop the listener thread.
        """
        rospy.loginfo("Disconnecting from sensor")
        if not self.sock:
            return
        self._listener_stop.set()
        try:
            # Closing socket will cause listener to exit
            self.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass
        self.sock = None
        if self._listener_thread:
            self._listener_thread.join(timeout=1.0)
        self._listener_thread = None

        # Wake up any waiting send_command callers with connection error
        with self._pending_lock:
            for seq, entry in self._pending_responses.items():
                with entry['cond']:
                    entry['response'] = SensorConnectionError("Disconnected")
                    entry['cond'].notify()
            self._pending_responses.clear()

        rospy.loginfo("Disconnected")

    def send_command(self, cmd_id, payload: bytes = b'', timeout: float = None):
        """
        Build and send a command packet synchronously, then wait for its response.

        Returns response payload bytes (user-data starting at payload byte 0).
        Raises SensorConnectionError or SensorProtocolError on error.
        """
        if not self.sock:
            raise SensorConnectionError("Not connected")
        if not isinstance(payload, (bytes, bytearray)):
            raise ValueError("Payload must be bytes")

        if timeout is None:
            timeout = self.rw_timeout

        # Generate an incremental 16-bit send sequence
        self._send_seq = (self._send_seq + 1) & 0xFFFF
        seq = self._send_seq

        # Build user payload: command id (1 byte) + payload
        cmd_id = cmd_id & 0xFF # limit cmd_id to 1 byte
        user_payload = bytes([cmd_id]) + payload
        length = len(user_payload)

        # Header: sync + seq (little endian) + length (little endian)
        header = bytearray()
        header += SYNC_BYTES
        header += struct.pack('<H', seq)
        header += struct.pack('<H', length)
        packet = bytes(header) + user_payload

        # Prepare pending response entry with a condition variable
        cond = threading.Condition()
        entry = {'cond': cond, 'response': None}
        with self._pending_lock:
            self._pending_responses[cmd_id] = entry

        # Send the packet
        rospy.logdebug("Sending command 0x%02X seq=%d len=%d", cmd_id, seq, length)
        rospy.logdebug(f"Sending message: {packet.hex()}")
        rospy.logdebug(f"Consisting of: {header.hex()} + {user_payload.hex()}")
        try:
            self.sock.settimeout(self.rw_timeout)
            self.sock.sendall(packet)
        except Exception as e:
            # clean up pending entry
            with self._pending_lock:
                self._pending_responses.pop(cmd_id, None)
            rospy.logerr("Send failed: %s", str(e))
            raise SensorConnectionError("Send failed: " + str(e))

        # Wait for response delivered by listener
        with cond:
            started = time.time()
            while True:
                remaining = timeout - (time.time() - started)
                if remaining <= 0:
                    # Timeout: remove pending entry and raise
                    with self._pending_lock:
                        self._pending_responses.pop(cmd_id, None)
                    rospy.logerr("Timeout waiting for response to cmd_id=%d", cmd_id)
                    raise SensorConnectionError("Timeout waiting for response")
                cond.wait(timeout=remaining)
                resp = entry['response']
                if resp is None:
                    # spurious wakeup, continue waiting
                    continue
                # If listener stored an exception object, raise it
                if isinstance(resp, Exception):
                    with self._pending_lock:
                        self._pending_responses.pop(cmd_id, None)
                    raise resp
                # normal bytes response
                with self._pending_lock:
                    self._pending_responses.pop(cmd_id, None)
                return bytes(resp)

    def start_tcp_stream(self, callback):
        """
        Send command 0x10 to start TCP process data streaming.
        Throws exception on error.
        """
        self._tcp_stream_callback = callback
        resp = self.send_command(0x10, b'')
        if len(resp) < 2:
            raise SensorProtocolError("Start stream response too short")
        err = resp[1]
        if err != 0:
            rospy.logwarn("Start stream returned error %d: %s", err, ERROR_CODES.get(err, "Unknown"))
            raise SensorAPIError(ERROR_CODES.get(err, "Unknown"))
        else:
            rospy.loginfo("Start stream accepted")

    def stop_tcp_stream(self):
        """
        Send command 0x11 to stop TCP process data streaming.
        Throws exception on error.
        """
        resp = self.send_command(0x11, b'')
        if len(resp) < 2:
            raise SensorProtocolError("Stop stream response too short")
        err = resp[1]
        if err != 0:
            rospy.logwarn("Stop stream returned error %d: %s", err, ERROR_CODES.get(err, "Unknown"))
            raise SensorAPIError(ERROR_CODES.get(err, "Unknown"))
        else:
            rospy.loginfo("Stop stream accepted")
            self._tcp_stream_callback = None
    
    def tare(self):
        """
        Send command 0x12 to tare the force-torque values.
        Throws exception on error.
        """
        resp = self.send_command(0x12, b'')
        if len(resp) < 2:
            raise SensorProtocolError("Tare response too short")
        err = resp[1]
        if err != 0:
            rospy.logwarn("Tare returned error %d: %s", err, ERROR_CODES.get(err, "Unknown"))
            raise SensorAPIError(ERROR_CODES.get(err, "Unknown"))
        else:
            rospy.loginfo("Tare successful")

    def reset_tare(self):
        """
        Send command 0x13 to reset tare (remove offset).
        Throws exception on error.
        """
        resp = self.send_command(0x13, b'')
        if len(resp) < 2:
            raise SensorProtocolError("Reset tare response too short")
        err = resp[1]
        if err != 0:
            rospy.logwarn("Reset tare returned error %d: %s", err, ERROR_CODES.get(err, "Unknown"))
            raise SensorAPIError(ERROR_CODES.get(err, "Unknown"))
        else:
            rospy.loginfo("Reset tare successful")

    def restart_module(self):
        """
        Send command 0x20 to restart the sensor module.
        Throws exception on error.
        """
        resp = self.send_command(0x20, b'')
        if len(resp) < 2:
            raise SensorProtocolError("Restart response too short")
        err = resp[1]
        if err != 0:
            rospy.logwarn("Restart returned error %d: %s", err, ERROR_CODES.get(err, "Unknown"))
            raise SensorAPIError(ERROR_CODES.get(err, "Unknown"))
        else:
            rospy.loginfo("Restart command accepted")

    def select_tool_settings(self, index: int):
        """
        Send command 0x30 to select a tool settings bank (0–3).
        Throws exception on error.
        """
        if not (0 <= index <= 3):
            raise ValueError("Tool settings index must be between 0 and 3")
        payload = struct.pack('<B', index)
        resp = self.send_command(0x30, payload)
        if len(resp) < 2:
            raise SensorProtocolError("Tool settings response too short")
        err = resp[1]
        if err != 0:
            rospy.logwarn("Tool settings select returned error %d: %s", err, ERROR_CODES.get(err, "Unknown"))
            raise SensorAPIError(ERROR_CODES.get(err, "Unknown"))
        else:
            rospy.loginfo("Tool settings bank %d selected", index)
    
    def set_noise_filter(self, filter_number: int):
        """
        Send command 0x31 to select noise reduction filter.
        Valid values: 0–4 (window sizes: 1, 2, 4, 8, 16).
        Throws exception on error.
        """
        if not (0 <= filter_number <= 4):
            raise ValueError("Filter number must be between 0 and 4")
        payload = struct.pack('<B', filter_number)
        resp = self.send_command(0x31, payload)
        if len(resp) < 2:
            raise SensorProtocolError("Noise filter response too short")
        err = resp[1]
        if err != 0:
            rospy.logwarn("Noise filter set returned error %d: %s", err, ERROR_CODES.get(err, "Unknown"))
            raise SensorAPIError(ERROR_CODES.get(err, "Unknown"))
        else:
            rospy.loginfo("Noise filter %d selected", filter_number)

    def start_udp_stream(self, callback):
        """
        Open udp socket and start listening
        Send command 0x40 to start UDP process data streaming.
        Throws exception on error.
        """

        if self._udp_running:
            return  # already running
        self._udp_running = True
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('', UDP_PORT))
        s.settimeout(DEFAULT_CONNECT_TIMEOUT)
        self._udp_socket = s
        self._udp_listener_stop = threading.Event()
        self._udp_stream_callback = callback
        self._udp_thread = threading.Thread(target=self._udp_listener_loop, daemon=True)
        self._udp_thread.start()
    
        resp = self.send_command(0x40, b'')
        if len(resp) < 2:
            raise SensorProtocolError("Start UDP stream response too short")
        err = resp[1]
        if err != 0:
            rospy.logwarn("Start UDP stream returned error %d: %s", err, ERROR_CODES.get(err, "Unknown"))
            raise SensorAPIError(ERROR_CODES.get(err, "Unknown"))
        else:
            rospy.loginfo("UDP streaming started")

    def stop_udp_stream(self):
        """
        Send command 0x41 to stop UDP process data streaming.
        Throws exception on error.
        """
        # cluse socket and stop threads
        if not self._udp_socket:
            return
        self._udp_listener_stop.set()
        if self._udp_thread:
            self._udp_thread.join()
        self._udp_running = False

        resp = self.send_command(0x41, b'')
        if len(resp) < 2:
            raise SensorProtocolError("Stop UDP stream response too short")
        err = resp[1]
        if err != 0:
            rospy.logwarn("Stop UDP stream returned error %d: %s", err, ERROR_CODES.get(err, "Unknown"))
        else:
            rospy.loginfo("UDP streaming stopped")
            self._udp_stream_callback = None

        try:
            self._udp_socket.close()
        except Exception:
            raise
        
        self._udp_socket = None
        self._udp_thread = None

    def read_parameter(self, index: int, subindex: int):
        """
        Send command 0xF0 to read a parameter by index and subindex.
        Returns value_bytes — throws exception on error
        """
        if not (0 <= index <= 0xFFFF):
            raise ValueError("Index must be 0–65535")
        if not (0 <= subindex <= 0xFF):
            raise ValueError("Subindex must be 0–255")
        payload = struct.pack('<HB', index, subindex)
        resp = self.send_command(0xF0, payload)
        if len(resp) < 5:
            raise SensorProtocolError("Read parameter response too short")
        err = resp[1]
        resp_index = struct.unpack_from('<H', resp, 2)[0]
        resp_subindex = resp[4]
        if resp_index != index or resp_subindex != subindex:
            rospy.logwarn("Read parameter response index/subindex mismatch")
        value = resp[5:] if err == 0 else b''
        if err != 0:
            rospy.logwarn("Read parameter returned error %d: %s", err, ERROR_CODES.get(err, "Unknown"))
            raise SensorAPIError(ERROR_CODES.get(err, "Unknown"))
        else:
            rospy.logdebug("Read parameter index=%d subindex=%d value_len=%d", index, subindex, len(value))
        return value

    def write_parameter(self, index: int, subindex: int, value: bytes):
        """
        Send command 0xF1 to write a parameter by index and subindex.
        Value must be a bytes object.
        Throws exception on error.
        """
        if not (0 <= index <= 0xFFFF):
            raise ValueError("Index must be 0–65535")
        if not (0 <= subindex <= 0xFF):
            raise ValueError("Subindex must be 0–255")
        if not isinstance(value, (bytes, bytearray)):
            raise ValueError("Value must be bytes")
        payload = struct.pack('<HB', index, subindex) + value
        resp = self.send_command(0xF1, payload)
        if len(resp) < 5:
            raise SensorProtocolError("Write parameter response too short")
        err = resp[1]
        resp_index = struct.unpack_from('<H', resp, 2)[0]
        resp_subindex = resp[4]
        if resp_index != index or resp_subindex != subindex:
            rospy.logwarn("Write parameter response index/subindex mismatch")
        if err != 0:
            rospy.logwarn("Write parameter returned error %d: %s", err, ERROR_CODES.get(err, "Unknown"))
            raise SensorAPIError(ERROR_CODES.get(err, "Unknown"))
        else:
            rospy.logdebug("Write parameter index=%d subindex=%d value_len=%d", index, subindex, len(value))

    def write_uint8(self, index: int, subindex: int, val: int):
        self.write_parameter(index, subindex, struct.pack('<B', val))

    def write_uint16(self, index: int, subindex: int, val: int):
        self.write_parameter(index, subindex, struct.pack('<H', val))

    def write_uint32(self, index: int, subindex: int, val: int):
        self.write_parameter(index, subindex, struct.pack('<I', val))

    def write_int32(self, index: int, subindex: int, val: int):
        self.write_parameter(index, subindex, struct.pack('<i', val))

    def write_float(self, index: int, subindex: int, val: float):
        self.write_parameter(index, subindex, struct.pack('<f', val))

    def write_bool(self, index: int, subindex: int, val: bool):
        self.write_parameter(index, subindex, struct.pack('<B', int(bool(val))))

    def write_enum(self, index: int, subindex: int, val: int):
        self.write_parameter(index, subindex, struct.pack('<B', val))

    def write_char(self, index: int, subindex: int, val: str):
        if not val or len(val) != 1:
            raise ValueError("CHAR must be a single ASCII character")
        self.write_parameter(index, subindex, struct.pack('<B', ord(val)))
    
    def decode_uint8(self, value):
        if len(value) == 1:
            return struct.unpack('<B', value)[0]
        else:
            raise ValueError("Value must contain 1 byte")
        
    def decode_uint16(self, value):
        if len(value) == 2:
            return struct.unpack('<H', value)[0]
        else:
            raise ValueError("Value must contain 2 bytes")
    
    def decode_uint32(self, value):
        if len(value) == 4:
            return struct.unpack('<I', value)[0]
        else:
            raise ValueError("Value must contain 4 bytes")

    def decode_int32(self, value):    
        if len(value) == 4:
            return struct.unpack('<i', value)[0]
        else:
            raise ValueError("Value must contain 4 bytes")

    def decode_float(self, value):    
        if len(value) == 4:
            return struct.unpack('<f', value)[0]
        else:
            raise ValueError("Value must contain 4 bytes")
    
    def decode_bool(self, value):
        if len(value) == 1:
            return  bool(struct.unpack('<B', value)[0])
        else:
            raise ValueError("Value must contain 1 byte")

    def read_uint8(self, index: int, subindex: int):
        value = self.read_parameter(index, subindex)
        return self.decode_uint8(value)

    def read_uint16(self, index: int, subindex: int):
        value = self.read_parameter(index, subindex)
        return self.decode_uint16(value)

    def read_uint32(self, index: int, subindex: int):
        value = self.read_parameter(index, subindex)
        return self.decode_uint32(value)

    def read_int32(self, index: int, subindex: int):
        value = self.read_parameter(index, subindex)
        return self.decode_int32(value)

    def read_float(self, index: int, subindex: int):
        value = self.read_parameter(index, subindex)
        return self.decode_float(value)

    def read_bool(self, index: int, subindex: int):
        value = self.read_parameter(index, subindex)
        return self.decode_bool(value)

    def read_enum(self, index: int, subindex: int):
        value = self.read_parameter(index, subindex)
        return self.decode_uint8(value)

    def read_char(self, index: int, subindex: int):
        value = self.read_parameter(index, subindex)
        return chr(self.decode_uint8(value))

    def read_string(self, index: int, subindex: int):
        value = self.read_parameter(index, subindex)
        return value.decode('ascii', errors='ignore').rstrip('\x00')

    def get_product_name(self):
        """
        Reads parameter 0x0001/0 — Product Name (CHAR[30]).
        Returns string.
        """
        return self.read_string(0x0001, 0)

    def get_product_text(self):
        """
        Reads parameter 0x0001/1 — Product Text (CHAR[30]).
        Returns string.
        """
        return self.read_string (0x0001, 1)

    def get_device_id(self):
        """
        Reads parameter 0x0001/2 — Device ID (UINT32).
        Returns int.
        """
        return self.read_uint32(0x0001, 2)

    def get_product_id(self):
        """
        Reads parameter 0x0001/3 — Product ID (UINT32).
        Returns int.
        """
        return self.read_uint32(0x0001, 3)

    def get_serial_number(self):
        """
        Reads parameter 0x0002 — Serial Number (CHAR[8]).
        Returns string.
        """
        return self.read_string(0x0002, 0)

    def get_hardware_version(self):
        """
        Reads parameter 0x0003/0 — Hardware Version (CHAR[8]).
        Returns string.
        """
        return self.read_string(0x0003, 0)

    def get_firmware_version(self):
        """
        Reads parameter 0x0003/1 — Firmware Version (CHAR[16]).
        Returns string.
        """
        return self.read_string(0x0003, 1)

    def get_internal_temperature(self):
        """
        Reads parameter 0x0035 — Internal Temperature (FLOAT).
        Returns float in °C.
        """
        return self.read_float(0x0035, 0)

    def get_tool_settings_locked(self):
        """
        Reads parameter 0x0060 — Tool Settings Unlocked (BOOL).
        Returns True if locked, False if unlocked. (Inverts internal function)
        """
        return not self.read_bool(0x0060, 0)

    def set_tool_settings_locked(self, locked: bool):
        """
        Writes parameter 0x0060 — Tool Settings Unlocked (BOOL). Inverts internal function.
        """
        self.write_bool(0x0060, 0, not locked)
    
    def get_tool_center_point(self):
        """
        Reads all 6 subindices of parameter 0x0061 — Tool Center Point.
        Returns dict with keys: tx, ty, tz, rx, ry, rz (values in meters/radians).
        """
        result = {}
        labels = ['tx', 'ty', 'tz', 'rx', 'ry', 'rz']
        for i, label in enumerate(labels):
            val = self.read_float(0x0061, i)
            result[label] = val
        return result

    def set_tool_center_point(self, tx, ty, tz, rx, ry, rz):
        """
        Writes all 6 subindices of parameter 0x0061 — Tool Center Point.
        Values: tx/ty/tz in meters, rx/ry/rz in radians.
        """
        values = [tx, ty, tz, rx, ry, rz]
        for i, val in enumerate(values):
            self.write_float(0x0061, i, val)


    def get_user_overrange_limits(self):
        """
        Reads all 12 subindices of parameter 0x0062 — Overrange Limits.
        Returns dict with keys: fx_pos, fx_neg, fy_pos, fy_neg, ..., tz_neg
        """
        result = {}
        labels = ['fx_pos', 'fx_neg', 'fy_pos', 'fy_neg', 'fz_pos', 'fz_neg',
                'tx_pos', 'tx_neg', 'ty_pos', 'ty_neg', 'tz_pos', 'tz_neg']
        for i, label in enumerate(labels):
            val = self.read_float(0x0062, i)
            result[label] = val
        return result

    def set_user_overrange_limits(self, **kwargs):
        """
        Writes any subset of overrange limits to parameter 0x0062.
        Accepts keyword args: fx_pos=..., fx_neg=..., ..., tz_neg=...
        Returns dict of error codes per written subindex.
        """
        label_to_index = {
            'fx_pos': 0, 'fx_neg': 1, 'fy_pos': 2, 'fy_neg': 3,
            'fz_pos': 4, 'fz_neg': 5, 'tx_pos': 6, 'tx_neg': 7,
            'ty_pos': 8, 'ty_neg': 9, 'tz_pos': 10, 'tz_neg': 11
        }
        result = {}
        for label, value in kwargs.items():
            if label not in label_to_index:
                result[label] = 'invalid_label'
                continue
            idx = label_to_index[label]
            self.write_float(0x0062, idx, value)


    def get_interface_vendor_name(self):
        """
        Reads parameter 0x1000/0 — Vendor Name (CHAR[30]).
        Returns string.
        """
        return self.read_string(0x1000, 0)


    def get_interface_vendor_text(self):
        """
        Reads parameter 0x1000/1 — Vendor Text (CHAR[30]).
        Returns string.
        """
        return self.read_string(0x1000, 1)

    def get_interface_product_id(self):
        """
        Reads parameter 0x1001/0 — Product ID (UINT32).
        Returns int.
        """
        return self.read_uint32(0x1001, 0)

    def get_interface_serial_number(self):
        """
        Reads parameter 0x3001/1 — Serial Number (CHAR[8]).
        Returns string.
        """
        return self.read_string(0x1001, 1)

    def get_interface_hardware_version(self):
        """
        Reads parameter 0x1002/0 — Hardware Version (CHAR[8]).
        Returns string.
        """
        return self.read_string(0x1002, 0)
    
    def get_interface_firmware_version(self):
        """
        Reads parameter 0x1002/1 — Firmware Version (CHAR[16]).
        Returns string.
        """ 
        return self.read_string(0x1002, 1)

    def get_interface_function_tag(self):
        """
        Reads parameter 0x1003/0 — Function Tag (CHAR[30]).
        Returns string.
        """
        return self.read_string(0x1003, 0)

    def get_interface_location_tag(self):
        """
        Reads parameter 0x1003/1 — Location Tag (CHAR[30]).
        Returns string.
        """
        return self.read_string(0x1003, 1)

    def get_udp_output_rate(self):
        """
        Reads parameter 0x1020/1 — UDP Output Rate (ENUM).
        Returns parameter value (int) (0-3).
        0 = 1 kHz, 1 = 500 Hz, 2 = 250 Hz, 3 = 100 Hz
        """
        return self.read_enum(0x1020, 0)

    def set_udp_output_rate(self, rate: int):
        """
        Writes parameter 0x1020/1 — UDP Output Rate (ENUM).
        Accepts values: 0 = 1 kHz, 1 = 500 Hz, 2 = 250 Hz, 3 = 100 Hz
        Returns parameter value (int).
        """
        if rate not in [0, 1, 2, 3]:
            raise ValueError("UDP rate must be 0 (1kHz), 1 (500Hz), 2 (250Hz), or 3 (100Hz)")
        self.write_enum(0x1020, 0, rate)

    def get_force_torque_scaling(self):
        """
        Reads parameter 0x1021 — Force/Torque Scaling Factor (UINT32).
        Returns parameter value (int).
        """
        return self.read_uint32(0x1021, 0)

    def set_force_torque_scaling(self, factor: int):
        """
        Writes parameter 0x1021 — Force/Torque Scaling Factor (UINT32).
        Valid range: 1 to 1,000,000
        Returns parameter value (int).
        """
        if not (1 <= factor <= 1_000_000):
            raise ValueError("Scaling factor must be between 1 and 1,000,000")
        self.write_uint32(0x1021, 0, factor)

    def get_use_static_ip(self):
        """
        Reads parameter 0x1030 — Use Static IP (BOOL).
        Returns False = static IP, True = DHCP.
        """
        return self.read_bool(0x1030, 0)

    def set_use_static_ip(self, use_dhcp: bool):
        """
        Writes parameter 0x1030 — Use Static IP (BOOL).
        Returns True = DHCP, False = static IP.
        """
        self.write_bool(0x1030, 0, use_dhcp)
    
    def get_customer_interface_type(self):
        """
        Reads parameter 0x1032/0 — Customer Interface Type (ENUM).
        Returns string label or None.
        ENUM values:
            0 = Unknown
            1 = EtherCat
            2 = Profinet
            3 = Ethernet/IP
            4 = Plain Ethernet
        """
        value = self.read_enum(0x1032, 0)
        iface_map = {
            0: "Unknown",
            1: "EtherCat",
            2: "Profinet",
            3: "Ethernet/IP",
            4: "Plain Ethernet"
        }
        return iface_map.get(value, f"Invalid ({value})")

    def _recv_all(self, n):
        """
        Read exactly n bytes from self.sock, respecting timeouts.
        Raises SensorConnectionError on timeout/closed socket.
        """
        data = bytearray()
        remaining = n
        while remaining > 0:
            try:
                chunk = self.sock.recv(remaining)
            except socket.timeout:
                raise SensorConnectionTimeout("Socket read timeout")
            except Exception as e:
                raise SensorConnectionError("Socket read error: " + str(e))
            if not chunk:
                raise SensorConnectionError("Socket closed by peer")
            data.extend(chunk)
            remaining -= len(chunk)
        return bytes(data)
    
    def _udp_listener_loop(self):
        while not self._udp_listener_stop.is_set():
            try:
                message = self._udp_socket.recvfrom(4096)
                data = message[0]
                # data = self._udp_socket.recvfrom(UDP_MESSAGE_SIZE)
                rospy.logdebug(f"got message of size {len(data)}: {data.hex()}")
            except socket.timeout:
                if not self._udp_listener_stop.is_set():
                    raise SensorConnectionTimeout("UDP socket read timeout")
            except Exception as e:
                if not self._udp_listener_stop.is_set():
                    raise SensorConnectionError("Socket read error: " + str(e))
            
            packet_id = data[HEADER_LEN] # packet id comes after Header
            if packet_id == PROCESS_PACKET_ID and self._udp_stream_callback:
                try:
                    self._udp_stream_callback(data[HEADER_LEN:])
                except Exception as e:
                    rospy.logerr("UDP Callback raised exception: %s", str(e))
                    continue
            else:
                # Unknown/unexpected packet (not a process packet and not a pending response).
                # Ignore but log at debug level.
                rospy.logdebug("Non-process, non-pending packet on UDP received with packet_id=0x%02X; ignored", packet_id)

    def _listener_loop(self):
        """
        Listener thread: continuously read packets (header + payload), then either:
          - deliver the packet to a waiting send_command (matching seq), or
          - parse it as process-data (packet_id == 0x01) and call callbacks.
        """
        rospy.logdebug("Listener thread started")
        while not self._listener_stop.is_set():
            try:
                # Always read header+payload atomically;
                header = self._recv_all(HEADER_LEN)
                if len(header) != HEADER_LEN:
                    rospy.logwarn("Invalid header length received: %d", len(header))
                    continue
                if header[0:2] != SYNC_BYTES:
                    rospy.logwarn("Invalid sync bytes in incoming packet")
                    # attempt to continue reading next packet
                    continue
                seq = struct.unpack_from('<H', header, 2)[0]
                length = struct.unpack_from('<H', header, 4)[0]
                payload = self._recv_all(length)
                rospy.logdebug(f"got message: {header.hex()} {payload.hex()}")
            except SensorConnectionTimeout as e:
                # timeout -> try again
                continue
            except SensorConnectionError as e:
                if not self._listener_stop.is_set():
                    rospy.logerr("Listener read error: %s", str(e))
                    # Deliver exception to any pending requester(s)
                    with self._pending_lock:
                        for s, entry in list(self._pending_responses.items()):
                            with entry['cond']:
                                entry['response'] = SensorConnectionError("Listener encountered read error")
                                entry['cond'].notify()
                        self._pending_responses.clear()
                continue
            except Exception as e:
                if not self._listener_stop.is_set():
                    rospy.logerr("Unexpected listener error: %s", str(e))
                    with self._pending_lock:
                        for s, entry in list(self._pending_responses.items()):
                            with entry['cond']:
                                entry['response'] = SensorConnectionError("Listener unexpected error")
                                entry['cond'].notify()
                        self._pending_responses.clear()
                continue

            if len(payload) != length:
                rospy.logwarn("Payload length mismatch expected=%d got=%d", length, len(payload))
                continue

            # First, check if there's a pending request waiting for this cmd_id
            cmd_id = payload[0]
            delivered_to_pending = False
            with self._pending_lock:
                pending_entry = self._pending_responses.get(cmd_id)
                if pending_entry is not None:
                    # Deliver raw payload bytes (user-data) to the waiting send_command
                    with pending_entry['cond']:
                        pending_entry['response'] = bytes(payload)
                        pending_entry['cond'].notify()
                    delivered_to_pending = True

            if delivered_to_pending:
                # response delivered to waiting thread; continue listening
                continue

            # Packet loss detection using header sequence number
            if self._expected_seq is None:
                self._expected_seq = (seq + 1) & 0xFFFF
            else:
                if seq != self._expected_seq:
                    rospy.logwarn("Packet loss detected expected=%d received=%d", self._expected_seq, seq)
                    self._expected_seq = (seq + 1) & 0xFFFF
                else:
                    self._expected_seq = (seq + 1) & 0xFFFF

            # Process user payload: payload[0] is packet id
            if length == 0:
                rospy.logdebug("Empty payload received")
                continue
            packet_id = payload[0]
            if packet_id == PROCESS_PACKET_ID and self._tcp_stream_callback:
                try:
                    self._tcp_stream_callback(payload)
                except Exception as e:
                    rospy.logerr("TCP Callback raised exception: %s", str(e))
                    continue
            else:
                # Unknown/unexpected packet (not a process packet and not a pending response).
                # Ignore but log at debug level.
                rospy.logdebug("Non-process, non-pending packet received with packet_id=0x%02X; ignored", packet_id)

        rospy.logdebug("Listener thread exiting")

    def parse_process_data(self, payload: bytes):
        """
        Parse process data payload.

        Payload (user-data) layout, indices relative to payload start:
        0   Packet-ID 0x01
        1-4 Status double word (4 bytes, little endian)
        5-8 Fx float32 little endian [N]
        9-12 Fy float32
        13-16 Fz float32
        17-20 Tx float32 [Nm]
        21-24 Ty float32
        25-28 Tz float32
        """
        # Minimum user payload length for process data is 29 bytes (0..28)
        if len(payload) < 29:
            raise SensorProtocolError("Process payload too short")
        fx = struct.unpack_from('<f', payload, 5)[0]
        fy = struct.unpack_from('<f', payload, 9)[0]
        fz = struct.unpack_from('<f', payload, 13)[0]
        tx = struct.unpack_from('<f', payload, 17)[0]
        ty = struct.unpack_from('<f', payload, 21)[0]
        tz = struct.unpack_from('<f', payload, 25)[0]

        proc = {
            'fx': float(fx),
            'fy': float(fy),
            'fz': float(fz),
            'tx': float(tx),
            'ty': float(ty),
            'tz': float(tz),
        }
        return proc

    def example_process_callback(self, data):
        # data is a dict with fx, fy, fz, tx, ty, tz
        parsed_data = self.parse_process_data(data)
        rospy.loginfo("Process data: fx=%.3f N fy=%.3f N fz=%.3f N tx=%.3f Nm ty=%.3f Nm tz=%.3f Nm",
                    parsed_data['fx'], parsed_data['fy'], parsed_data['fz'], parsed_data['tx'], parsed_data['ty'], parsed_data['tz'])

def main():
    rospy.init_node('schunk_fms_driver_node', anonymous=True, log_level=rospy.DEBUG)
    ip_param = rospy.get_param('~sensor_ip', "192.168.2.113")
    if not ip_param:
        rospy.logerr("Parameter ~sensor_ip not set")
        return
    driver = SchunkFmsDriver(connect_timeout=5.0, rw_timeout=5.0)
    try:
        driver.connect(ip_param, port=82)
    except Exception as e:
        rospy.logerr("Could not connect to sensor: %s", str(e))
        return

    try:
        err = driver.start_tcp_stream(driver.example_process_callback)
        if err != 0:
            rospy.logerr("Start stream failed with error %d: %s", err, ERROR_CODES.get(err, "Unknown"))
        else:
            rospy.loginfo("Streaming started, running until shutdown")
            rospy.spin()
    except rospy.ROSInterruptException:
        pass
    except Exception as e:
        rospy.logerr("Error during operation: %s", str(e))
    finally:
        try:
            driver.stop_tcp_stream()
        except Exception:
            pass
        driver.disconnect()

if __name__ == '__main__':
    main()
