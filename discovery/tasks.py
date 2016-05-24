from __future__ import absolute_import
import time
import threading
from scapy.all import *
from multiprocessing import Pool
from collections import deque

from celery import shared_task
from django.utils import timezone
from netaddr import IPNetwork
from django.db import IntegrityError
from django.db import connection as db_connection

from .models import System, Interface, Net, Socket, Connection, DNScache
from config.models import Origin 

import yappi

### fix to be able to spawn processes from a celery task
from celery.signals import worker_process_init
from multiprocessing import current_process

@worker_process_init.connect
def fix_multiprocessing(**kwargs):
    try:
        current_process()._config
    except AttributeError:
        current_process()._config = {'semprefix': '/mp'}

### fix to allow pickling from within a subproces
import dill

def run_dill_encoded(what):
    fun, args = dill.loads(what)
    return fun(*args)

def apply_async(pool, fun, args):
    return pool.apply_async(run_dill_encoded, (dill.dumps((fun, args)),))

###

origin_uuid="d44d8aa8c5ef495f992d7531336784fe"

def AddPacket(packets):
        def add_to_queue(pkt):
            packets.append(pkt)

        return add_to_queue

def RunCapture(interface, duration, packets):
        sniff(iface=interface, timeout=float(duration), store=0, prn=AddPacket(packets))

def ReadPcap(filepath, packets):
        sniff(offline=filepath,count=100000, prn=AddPacket(packets))
#        sniff(offline=filepath, prn=AddPacket(packets))

@shared_task
def PcapTask(filepath,origin_description):

        packets = deque()

        print("Starting to read pcap file: " + filepath)
        pythos_thread = threading.Thread(target=ReadPcap, args=(filepath, packets))
        pythos_thread.start()

        current_origin = Origin.objects.create( name="PCAP " + filepath,
                                                description=origin_description,
                                                sensor_flag=True,
                                                plant_flag=False )

        # For testing delete everything from previous captures
        Interface.objects.all().delete()
        
        num_processes = os.cpu_count()
        if not num_processes: num_processes = 2
 
        pool = Pool()

        print("Starting " + str(num_processes) + " worker processes.")
        while pythos_thread.is_alive() or packets:
                num_packets = len(packets)
                chunk_size = max((int)(num_packets/num_processes), 1000)

                print(str(num_packets) + " packets from pcap in queue.")

                if num_packets >= chunk_size:
                    # Get next packet chunk from the queue (FIFO)
                    chunk = deque()
                    for i in range(chunk_size):
                        chunk.append(packets.popleft())

                    apply_async(pool, packet_chunk, args=(chunk,current_origin))

                elif not pythos_thread.is_alive():
                    print("Processing last chunk.")
                    chunk = deque()
                    for i in range(num_packets):
                        chunk.append(packets.popleft())

                    apply_async(pool, packet_chunk, args=(chunk,current_origin))

                time.sleep(1)

        pythos_thread.join()
        pool.close()
        pool.join()
        print("Pcap has been processed successfully.")

@shared_task
def DiscoveryTask(interface, duration):

        global origin_uuid

        packets = deque()
        sleeptime = 0

        pythos_thread = threading.Thread(target=RunCapture, args=(interface, duration, packets))
        pythos_thread.start()

        # For testing delete everything from previous captures
        # Interface.objects.all().delete()
       
        lock = threading.Lock()
 
        try:
                current_origin = Origin.objects.get ( uuid=origin_uuid )
    
        except:
                print("Could not find specified origin: " + origin_uuid + " Aborting.")
                return

        while pythos_thread.is_alive() or packets:
                if packets:
                        sleeptime = 0

                        if threading.active_count() < 10:
                                # Get next packet from the queue (FIFO)
                                newPacket = packets.popleft()

                                threading.Thread(target=ProcessPacket,args=(newPacket,current_origin,lock)).start()
        
                        if len(packets) % 10 == 0:
                                print(str(len(packets)) + " packets from live capture in queue. " + str(threading.active_count()) + " thread(s) active.")
                else:
                        pass
#                        sleeptime += 1
#                        time.sleep(sleeptime)

def ProcessPacket(p,current_origin,lock):

        # see whether we like the package or not
        if not (p.haslayer(Ether) and p.haslayer(IP)):
                return

        # Save the source interface
        try:
            lock.acquire()

            src_interface = Interface.objects.get ( address_ether=p[Ether].src, address_inet=p[IP].src, origin=current_origin, ttl_seen=p[IP].ttl )

            setattr(src_interface, 'tx_pkts', src_interface.tx_pkts + 1)
            setattr(src_interface, 'tx_bytes', src_interface.tx_bytes + p.len)

            src_interface.save()

        except Interface.DoesNotExist:
                src_interface = Interface.objects.create( address_ether=p[Ether].src,
                                                          address_inet=p[IP].src,
                                                          tx_pkts=1,
                                                          tx_bytes=p.len,
                                                          ttl_seen=p[IP].ttl,
                                                          first_seen=timezone.now(),
                                                          last_seen=timezone.now(),
                                                          origin=current_origin )
        except AttributeError:
                print('Raised AttributeError when reading source from package:')
                print(p.summary)
                lock.release()
                return

        except IntegrityError:
                # Since we are not thread-safe,
                # we ignore attempts to create duplicate entries
                pass

        lock.release()

        # Save the destination interface
        try:
            lock.acquire()

            dst_interface = Interface.objects.get ( address_ether=p[Ether].dst, address_inet=p[IP].dst, origin=current_origin, ttl_seen=p[IP].ttl )

            setattr(dst_interface, 'rx_pkts', dst_interface.rx_pkts + 1)
            setattr(dst_interface, 'rx_bytes', dst_interface.rx_bytes + p.len)

            dst_interface.save()

        except Interface.DoesNotExist:
                dst_interface = Interface.objects.create( address_ether=p[Ether].dst,
                                                          address_inet=p[IP].dst,
                                                          rx_pkts=1,
                                                          rx_bytes=p.len,
                                                          ttl_seen=p[IP].ttl,
                                                          first_seen=timezone.now(),
                                                          last_seen=timezone.now(),
                                                          origin=current_origin )
        except AttributeError:
                print('Raised AttributeError when reading destination from package:')
                print(p.summary())
                lock.release()
                return

        except IntegrityError:
                # Since we are not thread-safe,
                # we ignore attempts to create duplicate entries
                pass

        lock.release()

        # Update the sockets
        try:
                lock.acquire()
                if p[Ether].type == 0x0800: # IPv4
                        src_socket, new_src_socket = Socket.objects.get_or_create( interface=src_interface,
                                                                                     port=p[IP].sport,
                                                                                     protocol_l4=p[IP].proto )

                        dst_socket, new_dst_socket = Socket.objects.get_or_create( interface=dst_interface,
                                                                                     port=p[IP].dport,
                                                                                     protocol_l4=p[IP].proto )

                elif p[Ether].type == 0x86DD: # IPv6
                        src_socket, new_src_socket = Socket.objects.get_or_create( interface=src_interface,
                                                                                     port=p[IP].sport,
                                                                                     protocol_l4=p[IP].nh )

                        dst_socket, new_dst_socket = Socket.objects.get_or_create( interface=dst_interface,
                                                                                     port=p[IP].dport,
                                                                                     protocol_l4=p[IP].nh )

                else:
                        # If we don't understand the protocol skip to the next package
                        lock.release()
                        return

        except AttributeError:
                print('Raised AttributeError when reading ports from package:')
                print(p.summary())
                lock.release()
                return

        except IntegrityError:
                # Since get_or_create is not thread-safe,
                # we ignore attempts to create duplicate entries
                pass

        lock.release()

        # Find DNS records
        if p[2].sport == 53:
                try:
                        updated_values_dnscache = { "name" : p[6].rrname,
                                                    "last_seen" : timezone.now() }

                        DNScache.objects.update_or_create( address_inet=p[6].rdata,
                                                           defaults=updated_values_dnscache )
                except AttributeError:
                        print('Raised AttributeError when reading DNS answer from package:')
                        print(p.summary())

                except TypeError:
                        print('Raised TypeError when reading DNS answer from package:')
                        print(p.summary())

                except IndexError:
                        # This was not a DNSRR. Disregard.
                        pass

        # Find DHCP ACKs
        if p[2].sport == 67:
                try:
                        if p[4].options[0][1] == 5: # DHCP ACK
                                ip_network = IPNetwork(p[1].dst)
                                ip_network.prefixlen=sum(
                                        [bin(int(x)).count('1') for x in p[4].options[5][1].split('.')] )

                                gateway, new_gateway = Interface.objects.get_or_create(
                                                                address_inet=p[4].options[7][1],
                                                                net=Net.objects.all()[0] )

                                name_server, new_name_server = Interface.objects.get_or_create(
                                                                address_inet=p[4].options[8][1],
                                                                net=Net.objects.all()[0] )

                                updated_values_net = { 'mask_inet' : p[4].options[5][1],
                                                       'address_bcast' : p[4].options[6][1],
                                                       'gateway' : gateway,
                                                       'name_server' : name_server }
                               
                                our_net, new_net = Net.objects.update_or_create(
                                                        address_inet=str(ip_network.network),
                                                        defaults=updated_values_net )
                except AttributeError:
                        print('Raised AttributeError when reading DHCP ACK from package:')
                        print(p.show())

        # Update the connections
        try:
                lock.acquire()

                con = Connection.objects.get( src_socket=src_socket,
                                              dst_socket=dst_socket,
                                              protocol_l567=p.proto )

                if p.proto == 6: # Check if this is a TCP connection 
                        setattr(con, 'seq', p.seq)

                setattr(con, 'tx_pkts', con.tx_pkts + 1)
                setattr(con, 'tx_bytes', con.tx_bytes + p.len)

                con.save()
                new_con = False

        except Connection.DoesNotExist:
                if p.proto==6:
                        con = Connection.objects.create( src_socket=src_socket,
                                                         dst_socket=dst_socket,
                                                         protocol_l567=p.proto,
                                                         first_seen=timezone.now(),
                                                         seq=p.seq )
                else:
                        con = Connection.objects.create( src_socket=src_socket,
                                                 dst_socket=dst_socket,
                                                 protocol_l567=p.proto,
                                                 first_seen=timezone.now() )

                new_con = True

        except IntegrityError:
                # Since we are not thread-safe,
                # we ignore attempts to create duplicate entries
                pass

        except Connection.MultipleObjectsReturned:
                # This shouldn't happen, but in case it does we will skip to the next package
                lock.release()
                return

        lock.release()

def packet_chunk(chunk, current_origin):
        lock = threading.Lock()

        print("Worker process with PID " + str(os.getpid()) + " has started.")
        
        while chunk:
            next_packet = chunk.popleft()
            ProcessPacket(next_packet, current_origin, lock)
        
        print("Worker process with PID " + str(os.getpid()) + " has finished.")
