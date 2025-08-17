import json
import os

USAGE_FILE = "usage.json"

def get_plan_limits(plan):
    limits = {
        "basic": {"max_duration": 6, "monthly_limit": 30},
        "pro": {"max_duration": 10, "monthly_limit": 100},
        "elite": {"max_duration": 10, "monthly_limit": 250}
    }
    return limits.get(plan)

def get_user_video_count(user_id):
    if not os.path.exists(USAGE_FILE):
        return 0

    with open(USAGE_FILE, "r") as f:
        data = json.load(f)

    return data.get(user_id, 0)

def increment_user_video_count(user_id):
    data = {}
    if os.path.exists(USAGE_FILE):
        with open(USAGE_FILE, "r") as f:
            data = json.load(f)

    data[user_id] = data.get(user_id, 0) + 1

    with open(USAGE_FILE, "w") as f:
        json.dump(data, f)
