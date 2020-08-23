<h1>weewx-aqua</h1>
<p>service for Aquagauge water level sensor controller, which supports up to
8 wireless sensors</p>

<p>configuration:</p>
<pre>
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
</pre>
<p>example weewx.conf entry</p>
<pre>
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
</pre>
