# Userspace polling driver for the Goodix GT911 touchscreen controller
A *hacky* solution created on a whim to allow for use of GT911-based touch panels on a system with I2C
but *not* GPIO pins (hence, no support for interrupts).

`driver.py` contains the bulk of the stuff. A systemd service file (`touchscreen.service`) is also available,
and assumes that the driver is placed at `/opt/touchscreen/driver.py`. By default this does the bad, bad thing
by running as root - be sure to create proper service users with proper permissions for production usage!

### License
MIT - see `license.md` for details