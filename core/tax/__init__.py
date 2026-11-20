"""Federal tax arithmetic for a missionary's tax packet (board chunk c36).

Pure: no reads beyond the yearly tables beside this file, no writes, no
model. Functions take cents and return cents. The yearly figures live in
``tables/<year>.toml``, each with its IRS source (``figures.py``).
"""
