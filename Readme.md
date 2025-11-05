# Driver for Schunk FTS force torque sensor

Supporting TCP and UDP stream (up to 1000Hz).

To start the node, run (adjust ip address and parameters in launch file)

```bash
roslaunch fts_driver_visus bringup_driver.launch
```

Test connection by pinging the ip address before.

Once connected successfully, the wrench values are published on `/wrench` topic. 
Services to tare and restart the sensor (and more) are available under `/fts`.



