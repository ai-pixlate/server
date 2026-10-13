import os, signal, sys, time
print("recovery start", os.environ.get("DATABASE_URL"), flush=True)
signal.signal(signal.SIGTERM, lambda *_: (print("recovery stop", flush=True), sys.exit(0)))
while True: time.sleep(1)
