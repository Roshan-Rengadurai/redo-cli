import os

# never read the developer's real shell history during tests
os.environ["HISTFILE"] = os.devnull
