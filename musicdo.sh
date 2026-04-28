#!/bin/bash
# Launch dedicated Brave window for music                                                                                                                                                                 
source ~/venv/bin/activate

brave-browser --remote-debugging-port=9222 https://music.youtube.com https://music.apple.com &                             

# Wait for the debug port to be ready
until curl -sf http://localhost:9222/json/version > /dev/null 2>&1; do
  sleep 0.5
done

# Launch musicDo
exec python3 ./musicdo.py 
