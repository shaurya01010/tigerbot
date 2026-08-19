import os
import sys
import time
from datetime import datetime

# Try importing cfonts, and install it if not present
try:
    from cfonts import render
except ImportError:
    os.system('pip install python-cfonts')
    from cfonts import render

# Render "TradeGenius07" with cfonts in hacker style colors
output = render('SHAURYA', colors=['green'], align='left')
print(output)

# Initialize history to keep track of previous periods and results
history = []

def typing_effect(text, delay=0.05):
    """Simulate typing effect for text."""
    for char in text:
        sys.stdout.write(char)
        sys.stdout.flush()
        time.sleep(delay)
    print()

def display_period_and_timer():
    last_minute = None
    remaining_seconds = 59

    # Define the pattern for SMALL and BIG
    pattern = ["SMALL", "SMALL", "BIG", "SMALL", "SMALL", "BIG", "SMALL", "BIG"]
    pattern_length = len(pattern)

    # Dark background styling for terminal (ANSI escape codes)
    print("\033[40m\033[92m")  # Start black background with green text

    while True:
        now = datetime.utcnow()

        # Only show connection sequence once at startup
        if last_minute is None:
            typing_effect("\n[Establishing Secure Connection...]", delay=0.03)
            time.sleep(0.5)
            typing_effect("[Encrypting Data Stream...]", delay=0.03)
            time.sleep(0.5)
            typing_effect("[Connected to Server | Awaiting Data]", delay=0.03)
            time.sleep(0.5)

        # Check if the minute has changed to update the period
        if now.minute != last_minute:
            last_minute = now.minute

            # Calculate total minutes since midnight
            total_minutes = now.hour * 60 + now.minute

            # Get the pattern for the current minute based on its position
            current_pattern = pattern[total_minutes % pattern_length]

            # Format the period number for the 1-minute interval
            period_1m = now.strftime("%Y%m%d") + "1000" + str(10001 + total_minutes)

            # Add the current period and result to history
            history.append((period_1m, current_pattern))
            if len(history) > 5:  # Keep only the last 5 entries for readability
                history.pop(0)

            # Clear previous output for cleaner display
            os.system('cls' if os.name == 'nt' else 'clear')
            print("\033[40m\033[92m")  # Reset styling after clear
            print(output)  # Re-print the banner

            # Simulate data retrieval with loading effect
            typing_effect("\n[Processing Data...]", delay=0.04)
            time.sleep(0.5)  # Simulate brief loading
            typing_effect("\033[32m[Data Retrieved Successfully]\033[0m", delay=0.04)

            # Display the current period and result in a table with hacker styling
            print(f"\n{'='*60}")
            print(f"|{'PERIOD':^28}|{'RESULT':^28}|")  # Header
            print(f"{'-'*60}")
            print(f"|{period_1m:^28}|{current_pattern:^28}|")  # Period and result
            print(f"{'='*60}\n")

            # Display the history of periods and results in a styled table format
            print("[Historical Data - Last 5 Periods]")
            print(f"{'='*60}")
            print(f"|{'PERIOD':^28}|{'RESULT':^28}|")
            print(f"{'-'*60}")
            for period, result in history:
                print(f"|{period:^28}|{result:^28}|")
            print(f"{'='*60}")

            # Reset the remaining seconds to 59 at the start of a new minute
            remaining_seconds = 59

        # Calculate minutes and seconds for display
        minutes_display = 0
        seconds_display = remaining_seconds

        # Format the countdown timer as "xx : xx"
        formatted_time = f"{minutes_display:02} : {seconds_display:02}"

        # Display the countdown timer
        sys.stdout.write(f"\r[Timer] Countdown: {formatted_time}")
        sys.stdout.flush()

        # Decrease remaining seconds
        remaining_seconds -= 1

        # Reset the countdown at the end of the minute
        if remaining_seconds < 0:
            remaining_seconds = 59

        # Wait 1 second before updating again
        time.sleep(1)

# Run the function
if __name__ == "__main__":
    try:
        display_period_and_timer()
    except KeyboardInterrupt:
        print("\n\nProgram terminated by user.")
        sys.exit(0)