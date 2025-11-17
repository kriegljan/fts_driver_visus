# Driver for Schunk FTS force torque sensor

Supporting TCP (20Hz) and UDP stream (up to 1000Hz).
Requires an FTS with Ethernet Interface (!= EthernetIP!)
Tested with Firmware 2.1.0.

### Build

```bash
catkin build fts_driver_visus
```

### Start

To start the node, run (adjust ip address and parameters in launch file)

```bash
roslaunch fts_driver_visus bringup_driver.launch
```

Test connection by pinging the ip address before.

Once connected successfully, the wrench values are published on `/wrench` topic. 
Services to tare and restart the sensor (and more) are available under `/fts`.

### Documentation

Consider [FTS Ethernet FW2.1.0 Commissioning instructions.pdf](https://d16vz4puxlsxm1.cloudfront.net/asset/076200133045-Prod/document_d0oo7uv2u51bpcp8kkr6ftfj6s/FTS%20Ethernet%20FW210%20Inbetriebnahmeanleitung.pdf) from [official Schunk website](https://schunk.com/de/de/automatisierungstechnik/kraft-momenten-sensoren/fts/fts-056-600-11/p/000000000001598246?cspt0=tctp&cspc0=productTabComponent&csot0=downloads).

The python API contains all described functions. The ROS API lacks on a service for setting force limits.

### Acknowledgements

Supported by the Deutsche Forschungsgemeinschaft (DFG, German Research Foundation) under Germany's Excellence Strategy – EXC 2120/1 – 390831618,
and by DFG project 495135767 (joint Weave project with FWF I 5912-N).

