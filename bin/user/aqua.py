# ©2015-20 Graham Eddy <graham.eddy@gmail.com>
# Distributed under the terms of weewx's GNU Public License (GPLv3)
"""
aqua module provides weewx data service for Aquagauge controller.

AquagaugeSvc: class providing data service for Aquagauge data
Aquagauge: class facing Aquagauge controller serial interface
"""

import sys
if sys.version_info[0] < 3:
    raise ImportError("requires python3")
import configobj
import queue
import logging
import serial
import threading
import time

import weewx
import weewx.units
from weewx.engine import StdService
from weeutil.weeutil import to_int, to_bool

log = logging.getLogger(__name__)
version = "3.2"


class AquagaugeSvc(StdService):
    """
    service for Aquagauge water level sensor controller, which supports up to
    8 wireless sensors

    configuration:
    [Aquagauge]
        port                    # serial port device (no default)
        speed                   # port speed /baud (default: 2400)
        open_attempts_max       # max no. open attempts (default: 4)
        open_attempts_delay     # delay before re-open /secs (default: 2)
        open_attempts_delay_long # long attempts delay /secs (default: 1800)
            # if opening port fails, retry open up to {open_attempts_max} times,
            # with {open_attempts_delay} secs between attempts. if even this
            # fails, back off for a further {open_attempts_delay_long} secs
            # before trying all over again. keep trying until port open
            # succeeds. read failure is handled by closing and re-opening the
            # port.
        log_success             # override global 'log_success'
        [[ _sensor_id_ ]]       # sensor no. (0-7)
            data_type           # data_type in LOOP record to be written
                                # (no default)
            unit                # unit of driver's provided value (no default)
        [[ _sensor_id_ ]] ...   # multiple sensors, up to sensor #7...
            # sensors with bad configurations are skipped.
            # at least one sensor must be enabled i.e. have {data_type} defined
            # otherwise the service exits.

    example weewx.conf entry
    [Aquagauge]
        port = /dev/ttyUSB0
        #speed = 2400
        #open_attempts_max = 4
        #open_attempts_delay = 2
        #open_attempts_delay_long = 1800
        log_success = false
        [[ 0 ]]
            data_type = riverLevel
            unit = mm
        [[ 1 ]]
            data_type = extraTemp3
            unit = degree_C
    """

    def __init__(self, engine, config_dict):
        super(AquagaugeSvc, self).__init__(engine, config_dict)

        log.debug(f"{self.__class__.__name__}: starting (version {version})")

        # scalars in configuration
        if 'Aquagauge' not in config_dict:
            log.error(f"{self.__class__.__name__}: Aquagauge section not found")
            return      # slip away without becoming a packet listener
        svc_sect = config_dict['Aquagauge']

        try:
            port = svc_sect['port']
            speed = to_int(svc_sect.get('speed', 2400))
            open_attempts_max = to_int(svc_sect.get('open_attempts_max', 4))
            open_attempts_delay = to_int(svc_sect.get('open_attempts_delay', 2))
            open_attempts_delay_long = \
                        to_int(svc_sect.get('open_attempts_delay_long', 1800))
            if 'log_success' in svc_sect:
                self.log_success = to_bool(svc_sect['log_success'])
            else:  # use global value
                self.log_success = \
                                to_bool(config_dict.get('log_success', False))
        except KeyError as e:
            log.error(f"{self.__class__.__name__}: config: missing {e.args[0]}")
            return      # slip away without becoming a packet listener
        except ValueError as e:
            log.error(f"{self.__class__.__name__}: config: {e.args[0]}")
            return      # slip away without becoming a packet listener

        # configure sensor sub-sections
        self.data_types = [None for _i in range(Aquagauge.MAX_SENSORS)]
                                            # maps sensor_id -> data_type
        self.units = self.data_types[:]     # maps sensor_id -> unit
        self.unit_groups = self.data_types[:] # maps sensor_id -> unit_group
        sensor_defs_count = sensor_count = 0
        for sensor_id, sensor_sect in svc_sect.items():
            if isinstance(sensor_sect, configobj.Section):
                sensor_defs_count += 1
                try:
                    sensor_id = to_int(sensor_id)
                    data_type = sensor_sect['data_type']
                    unit = sensor_sect['unit']
                    unit_group = weewx.units.obs_group_dict[data_type]
                except IndexError:
                    log.warning(f"{self.__class__.__name__}: config: bad"
                                f" sensor_id: {sensor_id}")
                except KeyError as e:
                    log.warning(f"{self.__class__.__name__}: config:"
                                f" sensor_id={sensor_id}: lacking {e.args[0]}")
                else:
                    self.data_types[sensor_id] = data_type
                    self.units[sensor_id] = unit
                    self.unit_groups[sensor_id] = unit_group
                    sensor_count += 1
        if sensor_count <= 0:
            log.error(f"{self.__class__.__name__}: no sensors enabled")
            return      # slip away without becoming a packet listener

        # create 'stop' signal for threads
        self.stop = threading.Event()

        # create queue between engine and device threads
        self.q = queue.Queue()

        # create device
        device = Aquagauge(port, speed, open_attempts_max,
                           open_attempts_delay, open_attempts_delay_long)

        # spawn acquirer thread
        try:
            self.acquirer = threading.Thread(target=device.run,
                                             args=(self.q, self.stop))
            self.acquirer.start()
        except threading.ThreadError as e:
            log.error(f"{self.__class__.__name__}: thread failed: {repr(e)}")
            return      # slip away before becoming a packet listener

        # start listening to new packets
        self.bind(weewx.NEW_LOOP_PACKET, self.new_loop_record)
        log.info(f"{self.__class__.__name__} started (version {version}):"
                     f" {sensor_count} sensors enabled"
                     f", {sensor_defs_count - sensor_count} skipped")

    def new_loop_record(self, event):
        """handle LOOP record by inserting queued readings"""

        # cannot unbind as packet listener, but can choose not to respond
        if self.stop.is_set():
            if weewx.debug > 1:
                log.debug(f"{self.__class__.__name__}.new_loop_record:"
                          f" stop.is_set")

        readings_count = 0
        try:
            while not self.q.empty():
                reading = self.q.get(block=False)
                self.update(reading, event.packet)
                        # over-write prior value is unlikely but benign
                if weewx.debug > 1:
                    log.debug(f"{self.__class__.__name__}.new_loop_record:"
                              f" packet={event.packet}")
                self.q.task_done()
                readings_count += 1
        except queue.Empty:
            if weewx.debug > 1:
                log.debug(f"{self.__class__.__name__}.new_loop_record:"
                          f" queue.Empty")
            pass        # corner case that can be ignored
        if readings_count > 0:
            if self.log_success or weewx.debug > 0:
                log.info(f"{self.__class__.__name__}: {readings_count}"
                         f" readings found")
        else:
            if weewx.debug > 1:
                log.debug(f"{self.__class__.__name__}.new_loop_record:"
                          f" {readings_count} readings found")

    def update(self, reading, packet):
        """apply a reading to the packet"""

        # update LOOP packet where sensor is enabled and has a value
        for sensor_id, value in enumerate(reading):
            if value is not None and self.data_types[sensor_id]:
                # convert to internal units
                raw_vt = (value, self.units[sensor_id],
                                                self.unit_groups[sensor_id])
                pkt_vt = weewx.units.convertStd(raw_vt, packet['usUnits'])

                # insert value into packet
                if weewx.debug > 0:
                    log.debug(f"{self.__class__.__name__}.update packet["
                              f"{self.data_types[sensor_id]}]={pkt_vt[0]}")
                packet[self.data_types[sensor_id]] = pkt_vt[0]

    def shutDown(self):
        """respond to request to shutdown gracefully"""
        log.info(f"{self.__class__.__name__}: shutdown")

        self.stop.set()         # signal all threads to stop
        self.acquirer.kill()    # poke hard at acquirer thread


class Terminate(Exception):
    """thread has been requested to stop"""


class Aquagauge:
    """driver for acquiring data from Auqagauge controller via serial interface"""

    # a reading is a list of observed values, indexed by sensor_id
    #
    # number of sensors supported by controller
    MAX_SENSORS = 8

    # input records are formatted as
    #   reading ::= [ key value ]* "i"
    #   key ::= "a"|"b"|"c"|"d"|"e"|"f"|"g"|"h"
    #   value ::= digit [ digit ]*
    #   digit ::= "0"|"1"|"2"|"3"|"4"|"5"|"6"|"7"|"8"|"9"
    # where
    #   key is sensor_id offset by "a" e.g. c -> 2
    #   value is positive decimal integer
    #   there is no white space
    KEYS = b'abcdefgh'
    END_KEY = b'i'
    DIGITS = b'0123456789'

    def __init__(self, port, speed, open_attempts_max,
                       open_attempts_delay, open_attempts_delay_long):

        self.port = port
        self.speed = speed
        self.open_attempts_max = open_attempts_max
        self.open_attempts_delay = open_attempts_delay
        self.open_attempts_delay_long = open_attempts_delay_long

        self.f = None
        self.timer = None
        self.stop = None

    def run(self, q, stop):
        """insert readings from Aquagauge driver onto queue"""
        if weewx.debug > 1:
            log.debug(f"{self.__class__.__name__}.run start")

        self.stop = stop

        try:
            ch = self.getc()
            while not stop.is_set():
                reading = [None for _i in range(Aquagauge.MAX_SENSORS)]

                while True:         # process all fields of one record

                    # check for completion of a record
                    if ch == Aquagauge.END_KEY:
                        q.put(reading)
                        ch = self.getc()
                        break       # record finished

                    # identify key
                    if ch not in Aquagauge.KEYS:
                        log.warning(f"{self.__class__.__name__}: invalid key {ch}")
                        ch = self.getc()
                        continue    # field finished (failed)
                    sensor_id = Aquagauge.KEYS.index(ch)
                    ch = self.getc()

                    # calculate value
                    if ch not in Aquagauge.DIGITS:
                        log.warning(f"{self.__class__.__name__}: missing value")
                        continue    # field finished (failed)
                    value = Aquagauge.DIGITS.index(ch)
                    while True:
                        ch = self.getc()
                        if ch not in Aquagauge.DIGITS:
                            break   # value finished (success)
                        value = 10*value + Aquagauge.DIGITS.index(ch)
                    reading[sensor_id] = value

        except Terminate as e:
            if weewx.debug > 0:
                log.debug(f"{self.__class__.__name__}.run: terminated")
        if weewx.debug > 1:
            log.debug(f"{self.__class__.__name__}.run finish")

    def getc(self):
        """get next char, in robust fashion"""

        # open device if not already open
        if self.f is None:
            self.open()

        # before potential wait on read, check if we have been asked to stop
        if self.stop.is_set():
            raise Terminate("stop")

        # try to read next char
        try:
            ch = self.f.read(1)
            if ch:
                return ch       # finished (success)
            # EOF, so drop through to error recovery
            log.warning(f"{self.__class__.__name__}: EOF")
        except serial.SerialException as e:
            log.error(f"{self.__class__.__name__}: read error", exc_info=e)

        # error recovery = close device and allow it to be re-opened
        try:
            self.f.close()
        except serial.SerialException as e:
            log.error(f"{self.__class__.__name__}: close error", exc_info=e)
        self.f = None

    def open(self):
        """open device, in robust fashion"""

        while True:             # try forever...

            # have a few short attempts
            for attempt in range(self.open_attempts_max):

                # before potential uncancellable wait on port open, check
                # if we have been asked to stop
                if self.stop.is_set():
                    raise Terminate("stop")

                try:
                    # 8 bits, 1 stop bit, no parity, no xon/xoff, no rts/dtr
                    self.f = serial.Serial(port=self.port, baudrate=self.speed)
                    if weewx.debug > 0:
                        log.debug(f"{self.__class__.__name__}: {self.port}:"
                                  f" open succeeded")
                    return      # finished open (success)
                except ValueError as e:
                    log.error(f"{self.__class__.__name__}: bad port config: {repr(e)}")
                except serial.SerialException as e:
                    log.error(f"{self.__class__.__name__}: {e.args[1]}")
                self.delay(self.open_attempts_delay)

            # have a long delay and hope for external intervention
            log.warning(f"{self.__class__.__name__}: long sleep"
                        f" waiting for port problem to be cleared")
            self.delay(self.open_attempts_delay_long - self.open_attempts_delay)

    def delay(self, secs):
        """delay for a while using cancellable timer, or sleep if timer fails"""

        def nothing(): pass

        try:
            self.timer = threading.Timer(secs, nothing)
            self.timer.start()
            self.timer.join()       # cancellable :-)
            self.timer = None
        except threading.ThreadError as e:
            if weewx.debug > 0:
                log.debug(f"{self.__class__.__name__}.delay: timer failed"
                          f" {repr(e)}, call sleep")
            time.sleep(secs)

    def kill(self):
        """make best effort to kill thread"""

        if self.timer:
            self.timer.cancel()

