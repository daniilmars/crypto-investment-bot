import threading

# Using threading.Event for thread-safe pause/resume functionality
# This object can be safely accessed from different threads.
bot_is_running = threading.Event()
bot_is_running.set() # Bot starts in a running state by default
