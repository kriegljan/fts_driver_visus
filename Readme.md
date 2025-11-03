# Driver for Schunk FTS force torque sensor

Currently python only, tcp only (20Hz)

To start the node, run (adjust ip address in launch file)

```bash
roslaunch fts_driver_visus bringup_driver.launch
```

Test connection by pinging the ip address before.

Once connected successfully, the wrench values are published on `/wrench` topic. 
Services to tare and restart the sensor are available under `/fts`.



