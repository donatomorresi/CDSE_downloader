#!/bin/bash

# Check for the first argument (which would be the command like 'search')
case "$1" in
  search)
    # Shift to remove the 'search' argument and pass the rest to the python script
    shift
    cdse-search "$@"
    ;;
  download)
    # Shift to remove the 'download' argument and pass the rest to the python script
    shift
    cdse-download "$@"
    ;;
  *)
    echo "Unknown command: $1"
    echo "Available commands: search, download"
    exit 1
    ;;
esac
