#!/bin/bash
# Headless bristol ARP Odyssey.
# bristol's engine only instantiates an emulation when its GUI (brighton) connects,
# so we give brighton a virtual X display. Both idle cheaply (~4 xruns/12s measured).
export DISPLAY=:99
pkill -f "Xvfb :99" 2>/dev/null
sleep 1
Xvfb :99 -screen 0 640x480x16 >/dev/null 2>&1 &
XPID=$!
sleep 3
startBristol -odyssey -jack -register bristolody -voices 6 -gain 2 >/dev/null 2>&1
# bristol does not auto-connect its audio — wire it to the Pisound output
( sleep 8
  jack_connect bristolody:out_left  system:playback_1 2>/dev/null
  jack_connect bristolody:out_right system:playback_2 2>/dev/null ) &

# startBristol returns after spawning engine+gui; hold the unit open
while pgrep -f "bristol .*bristolody" >/dev/null; do sleep 5; done
kill $XPID 2>/dev/null
