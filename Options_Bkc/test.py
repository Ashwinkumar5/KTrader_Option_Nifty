from SmartApi import SmartConnect

smartApi = SmartConnect(api_key="gsTme4KJ")

# Search for any method related to market data, quotes, or LTP
matching_methods = [
    method for method in dir(smartApi) 
    if any(k in method.lower() for k in ['market', 'quote', 'data', 'ltp', 'get']) and not method.startswith('_')
]

print("Available data methods in your SDK version:")
for m in matching_methods:
    print(f" -> {m}")