"""Deterministic metadata for UI/state tests; never query the host GPU."""
CPU_HARDWARE = dict(ram_total_mb=16384, ram_available_mb=8192, cpu_threads=8,
                    gpus=[], gpu_query_error='')
