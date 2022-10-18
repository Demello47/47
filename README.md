# 47

GREP
cat text.txt | grep -oP '"\K[^"]+' | sort | grep / # The command selects everything in quotes.
