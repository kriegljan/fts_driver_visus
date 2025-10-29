#!/usr/bin/env python3
import rospy
from fts_driver_visus import SchunkFmsDriver  # Adjust import if needed

def main():
    rospy.init_node('schunk_fms_test_reader', anonymous=True)
    driver = SchunkFmsDriver(connect_timeout=5.0, rw_timeout=5.0)

    try:
        driver.connect("192.168.2.113", port=82)
        print("✅ Connected to sensor")

        # Read basic identity parameters
        print("\n🔍 Identity Parameters:")
        print("Product Name:", driver.get_product_name())
        print("Product Text:", driver.get_product_text())
        print("Device ID:", driver.get_device_id())
        print("Product ID:", driver.get_product_id())
        print("Serial Number:", driver.get_serial_number())
        print("Hardware Version:", driver.get_hardware_version())
        print("Firmware Version:", driver.get_firmware_version())

        # Read internal temperature
        print("\n🌡️ Internal Sensor Status:")
        print("Internal Temperature [°C]:", driver.get_internal_temperature())

        # Tool settings lock state
        print("\n🔐 Tool Settings Lock:")
        print("Locked:", driver.get_tool_settings_locked())

        # Tool center point
        print("\n📐 Tool Center Point:")
        tcp = driver.get_tool_center_point()
        for k, v in tcp.items():
            print(f"{k}: {v:.6f}" if v is not None else f"{k}: [error]")

        # Overrange limits
        print("\n🚨 Overrange Limits:")
        limits = driver.get_user_overrange_limits()
        for k, v in limits.items():
            print(f"{k}: {v:.3f}" if v is not None else f"{k}: [error]")

        # Interface Box info
        print("\n📦 Interface Box Parameters:")
        print("Vendor Name:", driver.get_interface_vendor_name())
        print("Vendor Text:", driver.get_interface_vendor_text())
        print("Product ID:", driver.get_interface_product_id())
        print("Serial Number:", driver.get_interface_serial_number())
        print("Hardware Version:", driver.get_interface_hardware_version())
        print("Firmware Version:", driver.get_interface_firmware_version())
        print("Function Tag:", driver.get_interface_function_tag())
        print("Location Tag:", driver.get_interface_location_tag())

        # Interface Box Configuration
        print("\n⚙️ Interface Box Configuration:")
        rate_map = {0: "1 kHz", 1: "500 Hz", 2: "250 Hz", 3: "100 Hz"}
        err, rate = driver.get_udp_output_rate()
        print("UDP Output Rate:", rate_map.get(rate, f"[invalid: {rate}]") if err == 0 else "[error]")

        err, scaling = driver.get_force_torque_scaling()
        print("Force/Torque Scaling Factor:", scaling if err == 0 else "[error]")

        err, use_dhcp = driver.get_use_static_ip()
        if err == 0:
            print("IP Mode:", "DHCP" if use_dhcp else "Static IP")
        else:
            print("IP Mode: [error]")

        print("Customer Interface Type:", driver.get_customer_interface_type())

    except Exception as e:
        print("❌ Error:", str(e))
    finally:
        driver.disconnect()
        print("\n🔌 Disconnected from sensor")

if __name__ == '__main__':
    main()
