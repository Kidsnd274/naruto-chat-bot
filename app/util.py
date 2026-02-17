import os

def is_docker():
    # Method 1: Check .dockerenv
    if os.path.exists('/.dockerenv'):
        return True
    
    # Method 2: Check cgroup
    try:
        with open('/proc/self/cgroup', 'r') as f:
            if 'docker' in f.read():
                return True
    except (IOError, PermissionError):
        pass
    
    return False