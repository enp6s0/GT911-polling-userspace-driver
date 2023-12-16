#!/usr/bin/env python3
from smbus2 import SMBus, i2c_msg
from evdev import UInput, AbsInfo, ecodes as e
import time, argparse

"""
GT911 programming guide: https://community.nxp.com/pwmxy87654/attachments/pwmxy87654/imx-processors/177678/1/GT911%20Programming%20Guide_v0.1%20(1).pdf

To look at device IDs / see test events: libinput debug-events
Raw event output: evtest /dev/input/eventX (x = numerical ID)

Known limitations/bugs:

    * Polling is kind of slow (especially so with multiple touch points)
    * Vertical screen orientation currently doesn't work with this under Wayland. X11 works with some xrandr hacks
    * The exit/cleanup function isn't really implemented yet
        * Eventual TODO: implement CTRL+C / CTRL+D / SIGTERM handlers to actually do that
        * Eventual-eventual TODO: better systemd integration so it actually works as a proper daemon

"""

class GT911:

    def __init__(self, busID = "/dev/i2c-touchscreen", device = 0x5D, scaling = 1, flipX = False, flipY = False, swapXY = False, debug = False):
        """
        Initializer function
        """
        # Debug mode?
        self.debug = debug

        # Save bus ID and device address
        self.busID = busID
        self.deviceAddress = int(device)
        self.__dp(f"I2C bus: {self.busID}, device address: {self.deviceAddress:02X}")

        # Flip axes?
        self.flipX = flipX
        self.flipY = flipY
        self.swapXY = swapXY
        self.__dp(f"Axis flip (X = {self.flipX}) (Y = {self.flipY}) (X/Y swap = {self.swapXY})")

        # Scaling factor
        self.scalingFactor = int(scaling)
        self.__dp(f"Coordinate scaling factor: {self.scalingFactor}")

        # Open I2C bus
        self.bus = SMBus(self.busID)

        # Try to talk to the screen and get initial data
        self.touchBoundary = self.__queryTouchBoundary()
        self.coordinateResolution = self.__queryCoordinateResolution()
        self.__dp(f"Screen touch boundary: {self.touchBoundary}")
        self.__dp(f"Coordinate resolution: {self.coordinateResolution}")

        # Hardcoded register IDs for touch points
        self.__tpRegisterID = {
            0 : {
                "track" : [0x81, 0x4F],
                "x" : [[0x81, 0x50], [0x81, 0x51]],
                "y" : [[0x81, 0x52], [0x81, 0x53]],
                "size" : [[0x81, 0x54], [0x81, 0x55]]
            },
            1 : {
                "track" : [0x81, 0x57],
                "x" : [[0x81, 0x58], [0x81, 0x59]],
                "y" : [[0x81, 0x5A], [0x81, 0x5B]],
                "size" : [[0x81, 0x5C], [0x81, 0x5D]]
            },
            2 : {
                "track" : [0x81, 0x5F],
                "x" : [[0x81, 0x60], [0x81, 0x61]],
                "y" : [[0x81, 0x62], [0x81, 0x63]],
                "size" : [[0x81, 0x64], [0x81, 0x65]]
            },
            3 : {
                "track" : [0x81, 0x67],
                "x" : [[0x81, 0x68], [0x81, 0x69]],
                "y" : [[0x81, 0x6A], [0x81, 0x6B]],
                "size" : [[0x81, 0x6C], [0x81, 0x6D]]
            },
            4 : {
                "track" : [0x81, 0x6F],
                "x" : [[0x81, 0x70], [0x81, 0x71]],
                "y" : [[0x81, 0x72], [0x81, 0x73]],
                "size" : [[0x81, 0x74], [0x81, 0x75]]
            }
        }

        # Touch track information
        self.__touchInfo = {}
        self.__previousTouchInfo = {}

        # Virtual touchscreen capabilities
        self.caps = {
            e.EV_ABS: [
                (e.ABS_MT_SLOT, AbsInfo(value=0, min=0, max=9, fuzz=0, flat=0, resolution=0)),
                (e.ABS_MT_TOUCH_MAJOR, AbsInfo(0, 0, 255, 0, 0, 0)),
                (e.ABS_MT_POSITION_X, AbsInfo(0, 0, int(self.coordinateResolution[0]), 0, 0, 0)),
                (e.ABS_MT_POSITION_Y, AbsInfo(0, 0, int(self.coordinateResolution[1]), 0, 0, 0)),
                (e.ABS_MT_TRACKING_ID, AbsInfo(0, 0, 65535, 0, 0, 0)),
                (e.ABS_X, AbsInfo(0, 0, int(self.coordinateResolution[0]), 0, 0, 0)),
                (e.ABS_Y, AbsInfo(0, 0, int(self.coordinateResolution[1]), 0, 0, 0))
            ],
            e.EV_KEY: [e.BTN_TOUCH, e.BTN_TOOL_FINGER]
        }

        # This allows the system to see our input as a touchscreen and not a touchpad
        self.props = {
            e.INPUT_PROP_DIRECT,
        }

        # The virtual touchscreen device itself
        self.ui = UInput(self.caps, name="enp6s0 GT911 Userspace Touchscreen", version=0x3, input_props = self.props)

        # Go into read loop
        self.__readLoop()

    def __dp(self, message):
        """
        Quick wrapper function to print a message only if debug flag is set
        """
        if self.debug:
            print(f"[Debug] {message}")

    def cleanup(self):
        """
        Cleanup/exit function
        """
        self.bus.close()
        self.ui.close()

    def __writeI2C(self, register, data):
        """
        A function that writes data to a register of the i2c device...

        register: The register address to write to, specified as a list (high to low)
        data: Data to be written. This can be a single integer or a list of integers
        """

        # Sanity checks
        assert type(register) == list, "Register ID must be specified as list!"
        assert isinstance(data, (int, list)), "Data must be an integer or a list of integers!"
        if isinstance(data, int):
            data = [data]  # Convert single integer to a list

        # Combine the register and data into a single message
        message = register + data

        # Write the message to the device
        write = i2c_msg.write(self.deviceAddress, message)
        self.bus.i2c_rdwr(write)

    def __readI2C(self, register, numBytes = 1):
        """
        A function that reads numBytes from register of the i2c device
        """

        # Sanity checks
        assert type(register) == list, "Register ID must be specified as list! (high to low)"
        assert type(numBytes) == int, "Number of bytes to read must be integer!"
        assert len(register) >= 1, "Must specify register location!"
        assert int(numBytes) >= 1, "Must read at least 1 byte!"

        # First, we write the register address to read from -> device
        write = i2c_msg.write(self.deviceAddress, register)
        self.bus.i2c_rdwr(write)

        # Then, we read the data block
        read = i2c_msg.read(self.deviceAddress, numBytes)
        self.bus.i2c_rdwr(read)

        # Now we have the data...
        if(numBytes > 1):
            return list(read)
        else:
            return list(read)[0] # hacky but easy way of doing it...

    def __readI2CMultiByteValue(self, registers, combine = True):
        """
        Wrapper function to read multiple one-byte values from different registers
        (this also handles combining stuff, automagically)

        registers should be specified as a list of lists, with registers starting
        from the highest (first) byte to read. Returned data will be shifted/combined
        into one number if the argument "combine" is set to true, otherwise this
        returns a list of all items individually.
        """
        # Sanity check
        if not all(isinstance(reg, list) for reg in registers):
            raise ValueError("Each register must be specified as a list")

        # Read values, one byte from each register
        readValues = []
        for register in registers:
            value = self.__readI2C(register, numBytes = 1)
            readValues.append(value)

        # Combine the values into a single number (LOW bytes first)
        if combine:
            combinedValue = 0
            for i, value in enumerate(readValues):
                combinedValue |= value << (8 * i)
            return combinedValue

        # Otherwise just return the list, as-is
        else:
            return readValues

    def __queryTouchBoundary(self):
        """
        Wrapper function to query the GT911 device and get X and Y output max values
        """
        xMax = self.__readI2CMultiByteValue([[0x80, 0x48], [0x80, 0x49]]) * self.scalingFactor
        yMax = self.__readI2CMultiByteValue([[0x80, 0x4A], [0x80, 0x4B]]) * self.scalingFactor

        if(self.swapXY):
            xMax, yMax = yMax, xMax

        return (xMax, yMax)

    def __queryCoordinateResolution(self):
        """
        Wrapper function to query for coordinate resolution
        """
        xRes = self.__readI2CMultiByteValue([[0x81, 0x46], [0x81, 0x47]]) * self.scalingFactor
        yRes = self.__readI2CMultiByteValue([[0x81, 0x48], [0x81, 0x49]]) * self.scalingFactor

        if(self.swapXY):
            xRes, yRes = yRes, xRes

        return (xRes, yRes)

    def __queryPoint(self, pointID):
        """
        Get data for a specific touch point
        """
        assert 0 <= pointID <= 4, "Invalid point ID"

        xCoordinate = self.__readI2CMultiByteValue(self.__tpRegisterID[pointID]["x"]) * self.scalingFactor
        yCoordinate = self.__readI2CMultiByteValue(self.__tpRegisterID[pointID]["y"]) * self.scalingFactor

        # Flip axis inputs?
        if(self.flipX):
            xCoordinate = self.coordinateResolution[0] - xCoordinate
        if(self.flipY):
            yCoordinate = self.coordinateResolution[1] - yCoordinate
        if(self.swapXY):
            xCoordinate, yCoordinate = yCoordinate, xCoordinate

        size = self.__readI2CMultiByteValue(self.__tpRegisterID[pointID]["size"])
        track = self.__readI2C(self.__tpRegisterID[pointID]["track"])

        return xCoordinate, yCoordinate, size, track

    def __eventCallback(self):
        """
        This gets called when there is a change in touchinfo, where we figure out what's going on
        and fire the correct events
        """

        # Detect new tracks
        newTracks = set(self.__touchInfo.keys()) - set(self.__previousTouchInfo.keys())
        for trackID in newTracks:
            self.__newTrack(trackID, self.__touchInfo[trackID])
        if len(newTracks) > 0:
            self.ui.syn()

        # Detect updated tracks
        commonTracks = set(self.__touchInfo.keys()).intersection(self.__previousTouchInfo.keys())
        for trackID in commonTracks:
            #if self.__touchInfo[trackID] != self.__previousTouchInfo[trackID]:
            self.__updateTrack(trackID, self.__touchInfo[trackID])
        if len(commonTracks) > 0:
            self.ui.syn()

        # Detect ended tracks
        endedTracks = set(self.__previousTouchInfo.keys()) - set(self.__touchInfo.keys())
        for trackID in endedTracks:
            self.__endTrack(trackID)
        if len(endedTracks) > 0:
            self.ui.syn()

        # Update previous state
        self.__previousTouchInfo = self.__touchInfo.copy()

    def __newTrack(self, trackID, info):
        """
        Event handler for new tracks
        """
        self.__dp(f"New track: {trackID} {info}")

        self.ui.write(e.EV_ABS, e.ABS_MT_SLOT, trackID)
        self.ui.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, trackID)
        self.ui.write(e.EV_ABS, e.ABS_MT_POSITION_X, info["x"])
        self.ui.write(e.EV_ABS, e.ABS_MT_POSITION_Y, info["y"])
        self.ui.write(e.EV_ABS, e.ABS_MT_TOUCH_MAJOR, info["size"])
        #self.ui.syn()


    def __updateTrack(self, trackID, info):
        """
        Event handler for updated tracks
        """
        self.__dp(f"Updated track: {trackID} {info}")

        self.ui.write(e.EV_ABS, e.ABS_MT_SLOT, trackID)
        self.ui.write(e.EV_ABS, e.ABS_MT_POSITION_X, info["x"])
        self.ui.write(e.EV_ABS, e.ABS_MT_POSITION_Y, info["y"])
        self.ui.write(e.EV_ABS, e.ABS_MT_TOUCH_MAJOR, info["size"])
        #self.ui.syn()

    def __endTrack(self, trackID):
        """
        Event handler for ended tracks
        """
        self.__dp(f"Track ended: {trackID}")

        self.ui.write(e.EV_ABS, e.ABS_MT_SLOT, trackID)
        self.ui.write(e.EV_ABS, e.ABS_MT_TRACKING_ID, -1)
        #self.ui.syn()

    def __readLoop(self):
        """
        Polling loop to read data from device
        """

        # Yes, this is a blocking and never-ending service loop
        while(True):

            # First, we read the status byte and parse it...
            statusByte = self.__readI2C([0x81, 0x4E])

            bufferStatus = bool(statusByte & 0b10000000)    # Buffer status - do we have something for the host to read?
            largeDetect = bool(statusByte & 0b01000000)     # Large detect - do we have a large-area touch (possibly palm) on the panel?
            proximityValid = bool(statusByte & 0b00100000)  # Proximity valid - IDK this is not documented
            haveKey = bool(statusByte & 0b00010000)         # Touch key (True if "active", False if "released")
            touchPoints = statusByte & 0b00001111           # Number of touch points active in this report

            if(bufferStatus):

                # There is something for us to read! For now, this simple "driver" ignores touch keys completely (don't even know what that is)
                if(touchPoints >= 1):

                    # Aha - we have touch points! Let's figure out where they are, shall we?
                    tracksThisRound = []
                    for i in range(touchPoints):
                        x, y, size, track = self.__queryPoint(i)
                        self.__touchInfo[track] = {
                            "x" : x,
                            "y" : y,
                            "size" : size,
                            "track": track
                        }
                        tracksThisRound.append(track)

                    # Remove missing tracks from info
                    toDel = [k for k in self.__touchInfo.keys() if k not in tracksThisRound]
                    for k in toDel:
                        del self.__touchInfo[k]

                    # Call event handler
                    self.__eventCallback()

                else:
                    # Zero touch points (last finger lifted?)
                    self.__touchInfo = {}
                    self.__eventCallback()

                # Clear buffer - we've read already
                self.__writeI2C([0x81, 0x4E], 0)

            # Throttle the loop a bit... 1KHz should be enough
            time.sleep(0.001)


if __name__ == "__main__":

    parser = argparse.ArgumentParser(description = "User-space driver daemon for the Goodix GT911 touchscreen controller")

    parser.add_argument("--scaling", type = int, nargs = "?", default = 1, help = "Coordinate system scaling factor")

    parser.add_argument("--flip-x", action = "store_true", help = "Flip X-axis")
    parser.add_argument("--flip-y", action = "store_true", help = "Flip Y-axis")
    parser.add_argument("--swap-xy", action = "store_true", help = "Send X as Y, and Y as X")

    parser.add_argument("--debug", action = "store_true", help = "Debug mode")

    # Parse arguments
    args = parser.parse_args()

    # Get things going for real
    gt911 = GT911(scaling = args.scaling, flipX = args.flip_x, flipY = args.flip_y, swapXY = args.swap_xy, debug = args.debug)
