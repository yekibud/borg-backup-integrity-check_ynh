version: 2
ethernets:
  nat:
    match:
      macaddress: "__MAC1__"
    dhcp4: true
  lab:
    match:
      macaddress: "__MAC2__"
    dhcp4: false
    addresses: ["__LAB_IP__/24"]
