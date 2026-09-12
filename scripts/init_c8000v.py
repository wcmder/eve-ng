"""Initialize a running c8000v using its EVE console."""
import sys
from eve_lab.device_console import main

if __name__ == '__main__':
    main(['init', *sys.argv[1:]])
