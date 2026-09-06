#!/bin/sh
# The Arducam IMX296 module answers on I2C right away but returns SENSOR_INFO = 0 for about 15 s
# after it gets power, so the driver's probe at ~12 s after boot fails ("invalid device model
# 0x0000"). The overlay keeps the module powered (regulator-always-on); this retries the bind
# until the sensor is found. Typically succeeds around 20-25 s after boot.
D=/sys/bus/i2c/drivers/imx296
for i in $(seq 1 40); do
  if [ -e $D/5-001a ] && ls /dev/v4l-subdev* >/dev/null 2>&1 && media-ctl -d /dev/media0 -p 2>/dev/null | grep -q imx296; then
    echo "imx296 present (attempt $i)"; exit 0
  fi
  echo 5-001a > $D/unbind 2>/dev/null; sleep 1; echo 5-001a > $D/bind 2>/dev/null; sleep 2
done
echo "imx296 still missing after 40 attempts"; exit 1
