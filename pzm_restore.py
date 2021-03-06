#!/usr/bin/env python3

import time
import datetime
import os
import re

import pzm_common
from pzm_common import execute_readonly_command, execute_command, check_zfs_pool, log, log_debug
from pzm_locking import lock, unlock


#Disc class for the restore function.
#Each disk has a Name/ID, latest snaoshot, destination (aka pool) and a vm/ct config file
class Disk:
    def parse_id(self):
        id = self.name.split('-')[1]
        return id

    def get_last_snapshot(self, hostname, backupname):
        rc, stdout, stderr = execute_readonly_command(['ssh', '-o', 'BatchMode yes', 'root@' + hostname, 'zfs', 'list', '-t', 'snapshot', '-H', '-o', 'name', self.full_name])
        if (rc != 0):
            log ("(SSH) ZFS command error: " + stderr)
            sys.exit(1)
        stdout = stdout.split('\n')
        for x in set(stdout).intersection(pzm_common.considered_empty):
            stdout.remove(x)
        last = [element for element in stdout if(backupname in element)]

        if len(last) > 0:
            return last[-1]
        else:
            log_debug (f"No Snapshot found for { self.name } with backupname { backupname } - skipping...")
            self.skip = True #Backupname not found, skip in favor for others
            return None

    def get_last_config(self, hostname, configs_path):
        rc, stdout, stderr = execute_readonly_command(['ssh', '-o', 'BatchMode yes', 'root@' + hostname, 'ls', '-l', configs_path])
        if (rc != 0):
            log ("(SSH) ls -l command error: " + stderr)
            sys.exit(1)
        stdout = stdout.split('\n')
        for x in set(stdout).intersection(pzm_common.considered_empty):
            stdout.remove(x)
        relevant_files = [element for element in stdout if(self.last_snapshot.split('@')[1] in element and self.id in element)]
        if len(relevant_files) > 0:
            last_config = relevant_files[-1].split(' ')[-1]
            if "qemu" in last_config:
                self.type = "qemu"
            if "lxc" in last_config:
                self.type = "lxc"
            return last_config
        else:
            log_debug (f"No Config file found for {self.full_name}")
            self.skip = True #No Config File for this disk found, skip
            return None

    def get_destination(self, hostname, configs_path):
        rc, stdout, stderr = execute_readonly_command(['ssh', '-o', 'BatchMode yes', 'root@' + hostname, 'cat', configs_path + '/' + self.last_config])
        if (rc != 0):
            log ("(SSH) Get config path command error: " + stderr)
            sys.exit(1)
        stdout = stdout.split('\n\n')[0] #Read only first block of Configfile
        stdout = stdout.split('\n')
        for x in set(stdout).intersection(pzm_common.considered_empty):
            stdout.remove(x)
        diskconfig = [element for element in stdout if (self.name in element)]
        disk = ""
        if len(diskconfig) == 1:
            disk = diskconfig[0].split(',')[0]
            disk = disk.split(':',1)[1].replace(' ','')
        elif len(diskconfig) > 1: #Must have used the new prepent-dataset-id flag of pve-zsync, as pve-zsync would not work in that case
            #we get the destination pool from full_names pre last dataset name which is the pve-storage id if it was sent with prepent-dataset-id
            disk = self.full_name.split('/')[-2] + ':'+ self.name

        rc, stdout, stderr = execute_readonly_command(['pvesm', 'path', disk])
        if (rc != 0):
            log ("pvesm command error: " + stderr)
            sys.exit(1)
        destination = stdout.split('\n')
        for x in set(destination).intersection(pzm_common.considered_empty):
            destination.remove(x)

        if self.type == 'lxc':
            destination = destination[0].split('/',1)[1]
        elif  self.type == 'qemu':
            destination = destination[0].split('/dev/zvol/',1)[1]
        else:
            destination = ""
        return destination

    def __init__(self, hostname, full_name, backupname, configs_path):
        self.restore = False
        self.rollback = False
        self.keep = False
        self.skip = False
        self.full_name = full_name
        self.name = full_name.split('/')[-1]
        self.id = self.parse_id()
        self.last_snapshot = self.get_last_snapshot(hostname, backupname)
        if self.skip: # Can be set in get_last_snapshot
            return
        self.last_config = self.get_last_config(hostname, configs_path)
        if self.skip: # Can be set in get_last_config
            return
        self.destination = self.get_destination(hostname, configs_path)


#Each CT/VM can have multiple disks. A disc group represents all disks of a VM/CT
class Disk_Group:
    def __init__(self, id, type, last_config):
        self.skip = False
        self.id = id
        self.disks = []
        self.type = type
        self.last_config = last_config

    def __eq__(self,other):
        if not isinstance(other, Disk_Group):
            # don't attempt to compare against unrelated types
            return NotImplemented
        return self.id == other.id


#Parses all zfs disks on the remote side (with an optional filter), and asks the user what should be done to each individual disk.
def gather_restore_data(args):
    zfs_disks = check_zfs_pool(args.hostname, args.zfs_source_pool).split('\n')
    zfs_disks = [element for element in zfs_disks if re.search('(basevol|subvol|vm)-\d+-disk-\d+', element)]

    if args.filter is not None:
        zfs_disks = [element for element in zfs_disks if args.filter in element]
    for x in set(zfs_disks).intersection(pzm_common.considered_empty):
        zfs_disks.remove(x)
    zfs_disk_objects = []

    if pzm_common.debug:
        print ("Disks found after filter: " + str(zfs_disks))

    for zfs_disk in zfs_disks:
        if args.zfs_source_pool + '/' in zfs_disk:
            zfs_disk_objects.append(Disk(args.hostname, zfs_disk, args.backupname, args.config_path))
            if zfs_disk_objects[-1].skip:
                zfs_disk_objects.pop()
    disk_groups = []
    for disk in zfs_disk_objects:
        if not Disk_Group(disk.id, None, None) in disk_groups:
            group = Disk_Group(disk.id, disk.type, disk.last_config)
            group.disks.append(disk)
            disk_groups.append(group)
        else:
            disk_groups[disk_groups.index(Disk_Group(disk.id, None, None))].disks.append(disk)


    for group in disk_groups:
        print ("ID: " + group.id)
        for disk in group.disks:
            input_data = input ("Restore Disk from " + disk.last_snapshot + " to " + disk.destination + "? (y/n): ")
            while not (input_data == 'y' or input_data == 'n'):
                input_data = input ("Please answer y/n: ")
            if input_data == 'y':
               disk.restore = True
        no_restore_disks = [element for element in group.disks if (element.restore == False)]
        restore_disks = [element for element in group.disks if (element.restore == True)]
        if len(group.disks) > len(restore_disks):
            if len(restore_disks) > 0:
                for no_restore_disk in no_restore_disks:
                    input_data = input ("Fate of " + no_restore_disk.full_name + " - Rollback to same timestamp or keep current data and destroy all newer snapshots? (rollback/keep): ")
                    while not (input_data == 'rollback' or input_data == 'keep'):
                        input_data = input ("Please answer rollback/keep: ")
                    if input_data == 'rollback':
                        no_restore_disk.rollback = True
                    elif input_data == 'keep':
                        no_restore_disk.keep = True
            else:
                group.skip = True
        if len(restore_disks) == 0:
            group.skip = True
    print ("\n\nPlease check restore configuration:")
    for group in disk_groups:
        if group.skip:
            print ("ID: " + group.id + " skipped!")
            continue
        print ("ID: " + group.id + ":")
        for disk in group.disks:
            if disk.restore:
                print ("RESTORE: " +  disk.name + " from " + disk.last_snapshot + " to " + disk.destination + ": ")
            elif disk.rollback:
                print ("ROLLBACK: " + disk.name + " to " + disk.destination + disk.last_snapshot.split('@')[1])
            elif disk.keep:
                print ("KEEP DATA: " + disk.destination)
    input_data = input ("\nIs the information correct? (y):")
    if input_data == 'y':
       return disk_groups
    else:
       return None

#Destroys newer snapshots than the given one. Needed if the data has to be kept, but the snapshots have to be synchronized
def destroy_newer_snapshots(args, destination, snapshot):
    rc, stdout, stderr = execute_readonly_command(['zfs', 'list', '-t', 'snapshot', '-H', '-o', 'name', destination])
    stdout = stdout.split('\n')
    index_of_snap = stdout.index(destination + '@' + snapshot.split('@')[1])
    snaps_to_delete = stdout[index_of_snap+1:]
    for x in set(snaps_to_delete).intersection(pzm_common.considered_empty):
        snaps_to_delete.remove(x)
    for snap in snaps_to_delete:
        if snap == "":
            continue
        execute_command(['zfs', 'destroy', snap])

#Checks if a dataset is encrypted
def zfs_is_encrypted(dataset):
    rc, stdout, stderr = execute_readonly_command(['zfs', 'get', 'encryption', '-H', '-o', 'value', dataset])
    if rc != 0:
        return False
    elif "off" in stdout:
        return False
    else:
        return True

#Main method for the restore function. Will restore a backup made with pve-zsync-manager or pve-zsync according to the given input in gather_restore_data
def restore(args, disk_groups):
    lock(args.hostname)
    for group in disk_groups:
        if group.skip:
            print ("VM/CT ID " + group.id + " skipped...")
            continue
        print ("VM/CT ID " + group.id + " preparing...")
        if (group.type == "lxc"):
            execute_command(['pct', 'shutdown', group.id])
            execute_command(['pct', 'set', group.id, '--lock=backup'])

            rc, stdout, stderr, pid = execute_command(['mv', '/etc/pve/lxc/' + group.id + '.conf', '/etc/pve/lxc/' + group.id + '.conf.backup'])
            #if rc != 0:
            #    print (stdout)
            #    print (stderr)
            #    continue

            rc, stdout, stderr, pid = execute_command(['scp', '-B', 'root@' + args.hostname + ':' + args.config_path + '/' + group.last_config, '/etc/pve/lxc/' + group.id + '.conf'])
            if rc != 0:
                print (stdout)
                print (stderr)
                execute_command(['mv', '/etc/pve/lxc/' + group.id + '.conf.backup', '/etc/pve/lxc/' + group.id + '.conf'])
                continue

        elif (group.type == "qemu"):
            execute_command(['qm', 'shutdown', group.id])
            execute_command(['qm', 'set', group.id, '--lock=backup'])

            rc, stdout, stderr, pid = execute_command(['mv', '/etc/pve/qemu-server/' + group.id + '.conf', '/etc/pve/qemu-server/' + group.id + '.conf.backup'])
            if rc != 0:
                print (stdout)
                print (stderr)
                continue

            rc, stdout, stderr, pid = execute_command(['scp', '-B', 'root@' + args.hostname + ':' + args.config_path + '/' + group.last_config, '/etc/pve/qemu-server/' + group.id + '.conf'])
            if rc != 0:
                print (stdout)
                print (stderr)
                execute_command(['mv', '/etc/pve/qemu-server/' + group.id + '.conf.backup', '/etc/pve/qemu-server/' + group.id + '.conf'])
                continue
        no_restore_count = 0

        for disk in group.disks:
            if disk.restore:
                print ("VM/CT ID " + group.id + " - restoring " + disk.destination)
                rc, stdout, stderr = execute_readonly_command(['zfs', 'list', disk.destination])
                if rc == 0:
                    rc, stdout, stderr, pid = execute_command(['zfs', 'destroy', '-r', disk.destination])
                    if rc != 0:
                        print (stdout)
                        print (stderr)
                        continue
                rc, stdout, stderr, pid = execute_command(['ssh -o \"BatchMode yes\" root@' + args.hostname + ' zfs send -Rw ' +  disk.last_snapshot + ' | zfs recv -F ' + disk.destination], shell=True)
                if stderr != "":
                    print (stdout)
                    print (stderr)
                    continue

                if args.keyfile is not None:
                    dataset_encrypted = zfs_is_encrypted(disk.destination)
                    parent_encrypted = zfs_is_encrypted(disk.destination.rsplit('/',1)[0])
                    if dataset_encrypted:
                        rc, stdout, stderr, pid = execute_command(['zfs', 'set', 'keylocation=file://' + args.keyfile, disk.destination])
                        if rc != 0:
                            print (stdout)
                            print (stderr)
                            continue
                        rc, stdout, stderr, pid = execute_command(['zfs', 'load-key', disk.destination])
                        if rc != 0:
                            print (stdout)
                            print (stderr)
                            continue
                    if parent_encrypted:
                        rc, stdout, stderr, pid = execute_command(['zfs', 'change-key', '-i', disk.destination])
                        if rc != 0:
                            print (stdout)
                            print (stderr)
                            continue

                rc, stdout, stderr, pid = execute_command(['zfs', 'mount', disk.destination])
                if rc != 0:
                    print (stdout)
                    print (stderr)
                    continue
            elif disk.rollback:
                no_restore_count = no_restore_count + 1
                print ("VM/CT ID " + group.id + " - rolling back " + disk.destination + " to " + disk.last_snapshot.split('@')[1])
                rc, stdout, stderr, pid = execute_command(['zfs', 'rollback', '-r', disk.destination + disk.last_snapshot.split('@')[1]])
                if rc != 0:
                    print (stdout)
                    print (stderr)
                    continue

            elif disk.keep:
                no_restore_count = no_restore_count + 1
                print ("VM/CT ID " + group.id + " - destroying newer snapshots than " + disk.last_snapshot.split('@')[1] + " on " + disk.destination)
                destroy_newer_snapshots(args, disk.destination, disk.last_snapshot)


        if group.type == "lxc":
            execute_command(['pct', 'unlock', group.id])
        elif group.type == "qemu":
            execute_command(['qm', 'unlock', group.id])

        ## Force Delete PVE Snapshots which are not on all disks
        if no_restore_count > 0:
            cleanup_disks = [ element for element in group.disks if not ( element.restore )]
            if group.type == "lxc":
                snaps_in_config = execute_readonly_command(['pct', 'listsnapshot', group.id])[1]
            elif group.type == "qemu":
                snaps_in_config = execute_readonly_command(['qm', 'listsnapshot', group.id])[1]
            snaps_in_config = snaps_in_config.split('\n')

            for x in set(snaps_in_config).intersection(pzm_common.considered_empty):
                snaps_in_config.remove(x)

            snapnames_in_config = []
            for snap_in_config in snaps_in_config:
                snapnames_in_config.append(snap_in_config.lstrip().split(' ')[1])
                #print ("Snapname: " + snap_in_config.lstrip().split(' ')[1])
            if "current" in snapnames_in_config:
                snapnames_in_config.pop(snapnames_in_config.index("current"))

            all_snaps_on_disks = []
            for disk in cleanup_disks:
                rc, stdout, stderr = execute_readonly_command(['zfs', 'list', '-t', 'snapshot', '-H', '-o', 'name', disk.destination])
                snapshots_on_disk = stdout.split('\n')
                for x in set(snapshots_on_disk).intersection(pzm_common.considered_empty):
                    snapshots_on_disk.remove(x)
                for snapshot_on_disk in snapshots_on_disk:
                    if not snapshot_on_disk.split('@')[1] in all_snaps_on_disks:
                        #print ("Snap on Disk: " + snapshot_on_disk.split('@')[1])
                        all_snaps_on_disks.append(snapshot_on_disk.split('@')[1])

            for snapname_in_config in snapnames_in_config:
                if not snapname_in_config in all_snaps_on_disks:
                    if group.type == "lxc":
                        print ("Deleting Snapshot " + snapname_in_config + " because it's not present on all disks")
                        execute_command(['pct', 'delsnapshot', group.id, snapname_in_config, '--force'])
                    elif group.type == "qemu":
                        print ("Deleting Snapshot " + snapname_in_config + " because it's not present on all disks")
                        execute_command(['qm', 'delsnapshot', group.id, snapname_in_config, '--force'])

        print ("VM/CT ID " + group.id + " finished!")
    unlock(args.hostname)
