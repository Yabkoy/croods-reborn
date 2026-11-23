from Crypto.Cipher import AES
key = bytes.fromhex("5354636f51776b61544679707449394639436f53704846786d336a5457716865")
d = AES.new(key, AES.MODE_CBC, iv=b"\0"*16).decrypt(open("input.lua","rb").read())
d = d[:-d[-1]] if 0 < d[-1] <= 16 else d   # padding
open("out.7z","wb").write(d)               # powinno zaczynać się od 37 7A BC AF 27 1C