# Driver for Schunk FTS force torque sensor

Supporting TCP (20Hz) and UDP stream (up to 1000Hz).

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


### Acknowledgements

Supported by the Deutsche Forschungsgemeinschaft (DFG, German Research Foundation) under Germany's Excellence Strategy – EXC 2120/1 – 390831618,
and by DFG project 495135767 (joint Weave project with FWF I 5912-N).

