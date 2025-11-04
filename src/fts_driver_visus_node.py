#!/usr/bin/env python3
import rospy
import threading
import traceback
from std_srvs.srv import Trigger, TriggerResponse
from geometry_msgs.msg import WrenchStamped
from fts_driver_visus.srv import SetNoiseFilterWindow, SetNoiseFilterWindowResponse

from fts_driver_visus_api.fts_driver_visus_api import SchunkFmsDriver, ERROR_CODES

class FtsRosNode:
    def __init__(self):
        rospy.init_node('fts_ros_node')

        # Parameters
        self.ip = rospy.get_param('~ip', "192.168.2.113")
        self.tcp_port = 82
        self.use_udp = rospy.get_param('~use_udp', False)
        self.frame_id = rospy.get_param('~frame_id', "fts_sensor")

        # Driver
        self.driver = SchunkFmsDriver()

        # Publisher
        self._wrench_pub = rospy.Publisher('/wrench', WrenchStamped, queue_size=1)

        # Stream state
        self._stream_lock = threading.Lock()
        self._stream_running = False
        self._stream_protocol = None  # 'tcp' or 'udp'

        # Services
        rospy.Service('start_tcp_stream', Trigger, self._handle_start_tcp)
        rospy.Service('stop_tcp_stream', Trigger, self._handle_stop_tcp)
        rospy.Service('start_udp_stream', Trigger, self._handle_start_udp)
        rospy.Service('stop_udp_stream', Trigger, self._handle_stop_udp)
        rospy.Service('tare', Trigger, self._handle_tare)
        rospy.Service('restart', Trigger, self._handle_restart)
        rospy.Service('reset_tare', Trigger, self._handle_reset_tare)
        rospy.Service('set_noise_filter_window', SetNoiseFilterWindow, self._handle_set_noise_filter_window)

        rospy.on_shutdown(self._on_shutdown)

        # Connect and print parameters once
        self._connect_and_print_parameters()

        # Auto-start stream according to use_udp
        proto = 'udp' if self.use_udp else 'tcp'
        ok, msg = self._start_stream_protocol(proto)
        if ok:
            rospy.loginfo("Auto-started %s stream", proto)
        else:
            rospy.logwarn("Auto-start stream failed: %s", msg)

    def _connect_and_print_parameters(self):
        rospy.loginfo("Connecting to FTS driver...")
        self.driver.connect(self.ip, self.tcp_port)
        rospy.loginfo(f"FTS Parameters:")
        rospy.loginfo(f"\tProduct Name: {self.driver.get_product_name()}")
        rospy.loginfo(f"\tProduct Text: {self.driver.get_product_text()}")
        rospy.loginfo(f"\tDevice ID: {self.driver.get_device_id()}")
        rospy.loginfo(f"\tProduct ID: {self.driver.get_product_id()}")
        rospy.loginfo(f"\tSerial Number: {self.driver.get_serial_number()}")
        rospy.loginfo(f"\tHardware Version: {self.driver.get_hardware_version()}")
        rospy.loginfo(f"\tFirmware Version: {self.driver.get_firmware_version()}")
        rospy.loginfo(f"\tInternal Temperature [°C]: {self.driver.get_internal_temperature()}")

        # Tool settings lock state
        rospy.loginfo("Tool Settings Lock:")
        rospy.loginfo(f"\tLocked: {self.driver.get_tool_settings_locked()}")

        # Tool center point
        rospy.loginfo("Tool Center Point:")
        tcp = self.driver.get_tool_center_point()
        for k, v in tcp.items():
            rospy.loginfo(f"\t{k}: {v:.6f}" if v is not None else f"{k}: [error]")

        # Overrange limits
        rospy.loginfo("Overrange Limits:")
        limits = self.driver.get_user_overrange_limits()
        for k, v in limits.items():
            rospy.loginfo(f"\t{k}: {v:.3f}" if v is not None else f"{k}: [error]")

        # Interface Box info
        rospy.loginfo("Interface Box Parameters:")
        rospy.loginfo(f"\tVendor Name: {self.driver.get_interface_vendor_name()}")
        rospy.loginfo(f"\tVendor Text: {self.driver.get_interface_vendor_text()}")
        rospy.loginfo(f"\tProduct ID: {self.driver.get_interface_product_id()}")
        rospy.loginfo(f"\tSerial Number: {self.driver.get_interface_serial_number()}")
        rospy.loginfo(f"\tHardware Version: {self.driver.get_interface_hardware_version()}")
        rospy.loginfo(f"\tFirmware Version: {self.driver.get_interface_firmware_version()}")
        rospy.loginfo(f"\tFunction Tag: {self.driver.get_interface_function_tag()}")
        rospy.loginfo(f"\tLocation Tag: {self.driver.get_interface_location_tag()}")

        # Interface Box Configuration
        rospy.loginfo("Interface Box Configuration:")
        rate_map = {0: "1 kHz", 1: "500 Hz", 2: "250 Hz", 3: "100 Hz"}
        err, rate = self.driver.get_udp_output_rate()
        rate_parsed = rate_map.get(rate, f"[invalid: {rate}]") if err == 0 else "[error]"
        rospy.loginfo(f"\tUDP Output Rate: {rate_parsed}")

        err, scaling = self.driver.get_force_torque_scaling()
        factor_parsed = scaling if err == 0 else "[error]"
        rospy.loginfo(f"\tForce/Torque Scaling Factor: {factor_parsed}")

        err, use_dhcp = self.driver.get_use_static_ip()
        ip_parsed = "DHCP" if use_dhcp else "Static IP"
        if err == 0:
            rospy.loginfo(f"\tIP Mode: {ip_parsed}")
        else:
            rospy.loginfo(f"\tIP Mode: [error]")

        rospy.loginfo(f"\tCustomer Interface Type: {self.driver.get_customer_interface_type()}")

    def _stream_callback_publish_wrench(self, data):
        try:
            msg = WrenchStamped()
            msg.header.stamp = rospy.Time.now()
            msg.header.frame_id = self.frame_id

            parsed_data = self.driver.parse_process_data(data)

            msg.wrench.force.x = parsed_data['fx']
            msg.wrench.force.y = parsed_data['fy']
            msg.wrench.force.z = parsed_data['fz']

            msg.wrench.torque.x = parsed_data['tx']
            msg.wrench.torque.y = parsed_data['ty']
            msg.wrench.torque.z = parsed_data['tz']

            self._wrench_pub.publish(msg)
        except Exception:
            rospy.logerr("Failed to publish WrenchStamped: %s", traceback.format_exc())

    def _start_stream_protocol(self, protocol):
        with self._stream_lock:
            if self._stream_running:
                return False, f"{self._stream_protocol} stream already running"
            try:
                if protocol == 'tcp':
                    err = self.driver.start_tcp_stream(callback=self._stream_callback_publish_wrench)
                else:
                    err = self.driver.start_udp_stream(callback=self._stream_callback_publish_wrench)
                if err != 0:
                    raise Exception(f"Failed, error code {err}: {ERROR_CODES.get(err, 'Unknown')}")
            except Exception as e:
                rospy.logerr("Failed to start %s stream: %s\n%s", protocol, str(e), traceback.format_exc())
                return False, str(e)
            self._stream_running = True
            self._stream_protocol = protocol
            return True, "started"

    def _stop_stream_protocol(self, protocol):
        with self._stream_lock:
            if not self._stream_running or self._stream_protocol != protocol:
                return False, "not running or different protocol"
            try:
                if protocol == 'tcp':
                    err = self.driver.stop_tcp_stream()
                else:
                    err = self.driver.stop_udp_stream()
                if err != 0:
                    raise Exception(f"Failed, error code {err}: {ERROR_CODES.get(err, 'Unknown')}")
            except Exception as e:
                rospy.logerr("Failed to stop %s stream: %s\n%s", protocol, str(e), traceback.format_exc())
            # assume that the stream has stop anyways (to allow for restart)
            self._stream_running = False
            self._stream_protocol = None
            return True, "stopped"

    def _handle_start_tcp(self, req):
        ok, msg = self._start_stream_protocol('tcp')
        return TriggerResponse(success=ok, message=msg)

    def _handle_stop_tcp(self, req):
        ok, msg = self._stop_stream_protocol('tcp')
        return TriggerResponse(success=ok, message=msg)

    def _handle_start_udp(self, req):
        ok, msg = self._start_stream_protocol('udp')
        return TriggerResponse(success=ok, message=msg)

    def _handle_stop_udp(self, req):
        ok, msg = self._stop_stream_protocol('udp')
        return TriggerResponse(success=ok, message=msg)

    def _handle_tare(self, req):
        try:
            err = self.driver.tare()
            if err != 0:
                return TriggerResponse(success=False, message=f"Failed, error code {err}: {ERROR_CODES.get(err, 'Unknown')}")
            return TriggerResponse(success=True, message="tare executed")
        except Exception as e:
            rospy.logerr("Tare failed: %s", traceback.format_exc())
            return TriggerResponse(success=False, message=str(e))

    def _handle_restart(self, req):
        try:
            err = self.driver.restart()
            if err != 0:
                return TriggerResponse(success=False, message=f"Failed, error code {err}: {ERROR_CODES.get(err, 'Unknown')}")
            return TriggerResponse(success=True, message="restart executed")
        except Exception as e:
            rospy.logerr("Restart failed: %s", traceback.format_exc())
            return TriggerResponse(success=False, message=str(e))

    def _handle_reset_tare(self, req):
        try:
            err = self.driver.reset_tare()
            if err != 0:
                return TriggerResponse(success=False, message=f"Failed, error code {err}: {ERROR_CODES.get(err, 'Unknown')}")
            return TriggerResponse(success=True, message="reset_tare executed")
        except Exception as e:
            rospy.logerr("Reset tare failed: %s", traceback.format_exc())
            return TriggerResponse(success=False, message=str(e))

    def _handle_set_noise_filter_window(self, req):
        try:
            valid_values = [1,2,4,8,16]
            if not req.window_size in valid_values:
                return SetNoiseFilterWindowResponse(success=False, message=f"Window size {req.window_size} not allowed! Allowed values: {valid_values}")
            err = self.driver.set_noise_filter(valid_values.index(req.window_size))
            if err != 0:
                return SetNoiseFilterWindowResponse(success=False, message=f"Failed, error code {err}: {ERROR_CODES.get(err, 'Unknown')}")
            return SetNoiseFilterWindowResponse(success=True, message="set_noise_filter_window executed")
        except Exception as e:
            rospy.logerr("Reset tare failed: %s", traceback.format_exc())
            return SetNoiseFilterWindowResponse(success=False, message=str(e))

    def _on_shutdown(self):
        rospy.loginfo("Shutting down FTS ROS node...")
        with self._stream_lock:
            running = self._stream_running
            proto = self._stream_protocol
        if running and proto:
            self._stop_stream_protocol(proto)
        self.driver.disconnect()


if __name__ == '__main__':
    node = FtsRosNode()
    rospy.spin()
