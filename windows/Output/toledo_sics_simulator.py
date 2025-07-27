import serial
import random
import argparse
import time
import sys

# Simulated state
tare_offset = 0.0

def generate_random_weight():
    # Random base weight between 100.00 and 200.00 g
    return random.uniform(100.0, 200.0)

def get_stable_weight():
    weight = generate_random_weight() - tare_offset
    return f"S S    {weight:8.2f} g"

def get_unstable_weight():
    weight = generate_random_weight() + random.uniform(-0.05, 0.05)
    return f"S D    {weight:8.2f} g"

def process_command(cmd):
    global tare_offset

    cmd = cmd.strip().upper()

    if cmd == "SI":
        return get_stable_weight()
    elif cmd == "S":
        return get_unstable_weight()
    elif cmd == "T":
        tare_offset = generate_random_weight()
        return "T_S"
    elif cmd == "Z":
        tare_offset = 0.0
        return "Z_S"
    elif cmd == "I0":
        return "I0 JE5002G"
    elif cmd == "I1":
        return "I1 Mettler-Toledo JE5002G - Simulated"
    else:
        return "?"

def main():
    parser = argparse.ArgumentParser(description="Mettler Toledo SICS Protocol Scale Simulator")
    parser.add_argument("--port", required=True, help="Serial port (e.g., COM5 or /dev/ttyUSB0)")
    parser.add_argument("--baudrate", type=int, default=9600, help="Baud rate (default: 9600)")
    args = parser.parse_args()

    try:
        print(f"Opening {args.port} at {args.baudrate} baud...")
        with serial.Serial(
            port=args.port,
            baudrate=args.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=1
        ) as ser:
            print("Listening for SICS commands... Press Ctrl+C to stop.")
            while True:
                line = ser.readline().decode().strip()
                if line:
                    print(f">> Received: {line}")
                    response = process_command(line)
                    ser.write((response + "\r\n").encode())
                    print(f"<< Replied:  {response}")
    except serial.SerialException as e:
        print(f"[ERROR] Could not open port {args.port}: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nSimulator stopped.")
        sys.exit(0)
    except Exception as e:
        print(f"[ERROR] Unexpected error: {e}")
        sys.exit(1)

if __name__ == '__main__':
    main()
