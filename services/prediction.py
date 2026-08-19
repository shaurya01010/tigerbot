
# Prediction logic is kept equivalent to the supplied coding.py.
from datetime import datetime

pattern = ["SMALL", "SMALL", "BIG", "SMALL", "SMALL", "BIG", "SMALL", "BIG"]

def get_prediction(period_no):
    now = datetime.utcnow()
    total_minutes = now.hour * 60 + now.minute
    current_pattern = pattern[total_minutes % len(pattern)]
    generated_period = now.strftime("%Y%m%d") + "1000" + str(10001 + total_minutes)
    return (
        "🎯 RESULT\n\n"
        f"Period: {period_no}\n"
        f"Result: {current_pattern}\n\n"
        f"System period: {generated_period}"
    )
