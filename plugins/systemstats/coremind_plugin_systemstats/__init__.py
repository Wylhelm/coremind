"""CoreMind system statistics sensor plugin.

Collects CPU, memory, and uptime metrics from the local host using ``psutil``
and forwards signed ``WorldEvent``s to the CoreMind daemon every 30 seconds.
"""
