#!/usr/bin/env python3

import time
import datetime
import os
import re
import sys

import pzm_common
from pzm_common import execute_readonly_command, execute_command, check_zfs_pool, log, log_debug
from pzm_locking import lock, unlock


def get_unique_name_from_config_line(config_line):
    #from for example rootfs: vmssd:subvol-100-disk-0,mountpoint=.... to vmssd:subvol-100-disk-0
    return config_line.split(',')[0].split(':',1)[1].replace(' ','')

#Disc class for the restore function.
#Each disk has a Name/ID, latest snaoshot, destination (aka pool) and a vm/ct config file
class Disk:
    def parse_id(self):
        id = self.name.split('-')[1]
        return id

    def get_unique_name(self, hostname, config_file):
        rc, stdout, stderr = execute_readonly_command(['ssh', '-o', 'BatchMode yes', 'root@' + hostname, 'cat', config_file])
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
            disk = get_unique_name_from_config_line(diskconfig[0])
        elif len(diskconfig) > 1: #Must have used the new prepent-dataset-id flag of pve-zsync, as pve-zsync would not work in that case
            #we get the destination pool from full_names pre last dataset name which is the pve-storage id if it was sent with prepent-dataset-id
            #Example: backuppool/vmsys/subvol-100-disk-0: self.full_name.split('/')[-2] will be "vmsys", self.name subvol-100-disk-0
            disk = self.full_name.split('/')[-2] + ':'+ self.name


        #disk: "<storage_pool>:<disk_identification>" this has to be unique for each disk
        self.unique_name = disk

    def get_destination(self):
        rc, stdout, stderr = execute_readonly_command(['pvesm', 'path', self.unique_name])
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

    def __init__(self):
        self.unique_name = ""
        self.skip = False


class Backed_Up_Disk(Disk):
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

    def __init__(self, hostname, full_name, backupname, configs_path):
        super().__init__()
        self.restore = False
        self.rollback = False
        self.keep = False
        self.full_name = full_name
        self.name = full_name.split('/')[-1]
        self.id = self.parse_id()
        self.last_snapshot = self.get_last_snapshot(hostname, backupname)
        if self.skip: # Can be set in get_last_snapshot
            return
        self.last_config = self.get_last_config(hostname, configs_path)
        if self.skip: # Can be set in get_last_config
            return
        self.get_unique_name(hostname, configs_path + '/' + self.last_config)
        self.destination = self.get_destination()

class Non_Backed_Up_Disk(Disk):
    def __init__(self, config_file, config_line, type):
        super().__init__()
        self.unique_name = get_unique_name_from_config_line(config_line)
        self.type = type
        self.config_line = config_line
        self.destination = self.get_destination()
        self.recreate = False


#Each CT/VM can have multiple disks. A disc group represents all disks of a VM/CT
class Disk_Group:
    def __init__(self, id, type):
        self.skip = False
        self.id = id
        self.backed_up_disks:list[Backed_Up_Disk] = []
        self.non_backed_up_disks:list[Non_Backed_Up_Disk] = []
        self.type = type

    def get_last_config(self):
        configs = [disk.last_config for disk in self.backed_up_disks]
        if configs is not None and len(configs) > 0:
            #100.conf.qemu.rep_backup-name_2021-11-27_00:13:50
            configs.sort(key = lambda x: '_'.join([x.split('_')[-2], x.split('_')[-1]]),reverse=True)
            return configs[0]
        else:
            return None

    # Parse additional mountpoints or disks, which are not yet it the disks list
    def find_non_backed_up_disks(self, hostname, configs_path):
        if self.get_last_config() == None:
            return #Without any config, we can't check the remote config....

        rc, stdout, stderr = execute_readonly_command(['ssh', '-o', 'BatchMode yes', 'root@' + hostname, 'cat', configs_path + '/' + self.get_last_config()])
        if (rc != 0):
            log ("(SSH) Get config path command error: " + stderr)
            sys.exit(1)
        stdout = stdout.split('\n\n')[0] #Read only first block of Configfile
        stdout = stdout.split('\n')
        for x in set(stdout).intersection(pzm_common.considered_empty):
            stdout.remove(x)
        all_disks = [element for element in stdout if f"-{self.id}-disk-" in element]


        #Only use disks, which were not found at the backup location
        parsed_non_backed_up_disk_config_lines = [element for element in all_disks if len([backed_up_disk for backed_up_disk in self.backed_up_disks if get_unique_name_from_config_line(element) in backed_up_disk.unique_name]) == 0]


        for config_line in parsed_non_backed_up_disk_config_lines:
            self.non_backed_up_disks.append(Non_Backed_Up_Disk(configs_path + '/' + self.get_last_config(), config_line, self.type))

    def __eq__(self,other):
        if not isinstance(other, Disk_Group):
            # don't attempt to compare against unrelated types
            return NotImplemented
        return self.id == other.id


#Parses all zfs disks on the remote side (with an optional filter), and asks the user what should be done to each individual disk.
def gather_restore_data(args):
    print ("Gathering data from remote side, please wait...")
    zfs_disks = check_zfs_pool(args.hostname, args.zfs_source_pool).split('\n')
    zfs_disks = [element for element in zfs_disks if re.search('(basevol|subvol|vm)-\d+-disk-\d+', element)]

    if args.filter is not None:
        zfs_disks = [element for element in zfs_disks if args.filter in element]
    for x in set(zfs_disks).intersection(pzm_common.considered_empty):
        zfs_disks.remove(x)

    if pzm_common.debug:
        print ("Disks found after filter: " + str(zfs_disks))

    zfs_found_disk_objects:list[Backed_Up_Disk] = []
    for zfs_disk in zfs_disks:
        if args.zfs_source_pool + '/' in zfs_disk:
            disk = Backed_Up_Disk(args.hostname, zfs_disk, args.backupname, args.config_path)
            if not disk.skip: #If it was skipped during detection, we don't need to bother anymore
                zfs_found_disk_objects.append(disk)

    disk_groups = []
    for disk in zfs_found_disk_objects:
        if not Disk_Group(disk.id, None) in disk_groups:
            group = Disk_Group(disk.id, disk.type)
            group.backed_up_disks.append(disk)
            disk_groups.append(group)
        else:
            disk_groups[disk_groups.index(Disk_Group(disk.id, None))].backed_up_disks.append(disk)

    print ("")
    for group in disk_groups:
        #Disks which were not found in the specified backup location, but are defined in the configuration
        group.find_non_backed_up_disks(args.hostname, args.config_path)

        print ("ID: " + group.id)
        for disk in group.backed_up_disks:
            input_data = input ("Restore Disk from " + disk.last_snapshot + " to " + disk.destination + "? (y/n): ").lower()
            while not (input_data == 'y' or input_data == 'n'):
                input_data = input ("Please answer y/n: ").lower()
            if input_data == 'y':
                disk.restore = True

        print ("")
        no_restore_disks = [element for element in group.backed_up_disks if (element.restore == False)]
        restore_disks = [element for element in group.backed_up_disks if (element.restore == True)]
        if len(group.backed_up_disks) > len(restore_disks):
            if len(restore_disks) > 0: #Only ask for other disks in a CT, if at minimum one disk has to be restored
                for no_restore_disk in no_restore_disks:
                    #Check if it exists locally or if it's in the config, if not, warn user about it later that the The VM/CT will be restored in a broken state
                    rc, stdout, stderr = execute_readonly_command(['zfs', 'list', no_restore_disk.destination])
                    if rc == 0 : #It it was found, ask for it's fate
                        input_data = input ("Fate of " + no_restore_disk.unique_name + " - Rollback to same timestamp or keep current data and destroy all newer snapshots? (rollback/keep): ").lower()
                        while not (input_data == 'rollback' or input_data == 'keep'):
                            input_data = input ("Please answer rollback/keep: ").lower()
                        if input_data == 'rollback':
                            no_restore_disk.rollback = True
                        elif input_data == 'keep':
                            no_restore_disk.keep = True
                    else: #If it was not found, check if it actually is defined in the config file
                        rc, stdout, stderr = execute_readonly_command(['ssh', '-o', 'BatchMode yes', 'root@' + args.hostname, 'cat', args.config_path + '/' + group.get_last_config()])
                        if no_restore_disk.unique_name not in stdout: #Disk is not in config file, we can safely skip this disk
                            no_restore_disk.skip = True
            else:
                group.skip = True
        if len(restore_disks) == 0:
            group.skip = True

        if not group.skip:
            for non_backed_up_disk in group.non_backed_up_disks:
                rc, stdout, stderr = execute_readonly_command(['zfs', 'list', non_backed_up_disk.destination])
                if rc != 0: #Only ask if a disk should be recreated, if it doesn't already exist locally
                    input_data = input ("Disk " + non_backed_up_disk.unique_name + " was not backed up. Should it be recreated? (y/n): ").lower()
                    while not (input_data == 'y' or input_data == 'n'):
                        input_data = input ("Please answer y/n: ").lower()
                    if input_data == 'y':
                        non_backed_up_disk.recreate = True
                else:
                    non_backed_up_disk.skip = True

    print ("\n\nPlease check restore configuration:")
    for group in disk_groups:
        if group.skip:
            print ("ID: " + group.id + " skipped!")
            continue
        print ("ID: " + group.id + ":")
        for backed_up_disk in group.backed_up_disks:
            if backed_up_disk.restore:
                print ("RESTORE: " +  backed_up_disk.unique_name + " from " + backed_up_disk.last_snapshot + " to " + backed_up_disk.destination + ": ")
            elif backed_up_disk.rollback:
                print ("ROLLBACK: " + backed_up_disk.unique_name + " to " + backed_up_disk.destination + '@' + backed_up_disk.last_snapshot.split('@')[1])
            elif backed_up_disk.keep:
                print ("KEEP DATA: " + backed_up_disk.unique_name)
            elif backed_up_disk.skip:
                print ("SKIP: " + backed_up_disk.unique_name)
            else: #Doesn't exist locally, and should not be restored either, and is also not skipped because it isn't defined in the config
                print (pzm_common.bcolors.WARNING + "WARNING: Disk " + backed_up_disk.unique_name + " does not exist locally and was set to don't restore. The " + ("CT" if group.type == "lxc" else "VM") + " will be restored but the config will most likely be broken!" + pzm_common.bcolors.ENDC)
        for non_backed_up_disk in group.non_backed_up_disks:
            if non_backed_up_disk.recreate:
                print ("RECREATE: " + non_backed_up_disk.unique_name + " to " + non_backed_up_disk.destination)
            elif not non_backed_up_disk.skip:
                print ("DON'T RECREATE: " + non_backed_up_disk.unique_name)

    input_data = input ("\nIs the information correct? (y):".lower())
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

        ###### Shutdown VM/CT, lock it so the config won't be altered by PVE, back up the old config if exists, and copy over the backup config
        if (group.type == "lxc"):
            execute_command(['pct', 'shutdown', group.id])
            execute_command(['pct', 'set', group.id, '--lock=backup'])

            rc, stdout, stderr, pid = execute_command(['mv', '/etc/pve/lxc/' + group.id + '.conf', '/etc/pve/lxc/' + group.id + '.conf.backup'])
            #if rc != 0:
            #    print (stdout)
            #    print (stderr)
            #    continue

            rc, stdout, stderr, pid = execute_command(['scp', '-B', 'root@' + args.hostname + ':' + args.config_path + '/' + group.get_last_config(), '/etc/pve/lxc/' + group.id + '.conf'])
            if rc != 0:
                print (stdout)
                print (stderr)
                execute_command(['mv', '/etc/pve/lxc/' + group.id + '.conf.backup', '/etc/pve/lxc/' + group.id + '.conf'])
                continue

        elif (group.type == "qemu"):
            execute_command(['qm', 'shutdown', group.id])
            execute_command(['qm', 'set', group.id, '--lock=backup'])

            rc, stdout, stderr, pid = execute_command(['mv', '/etc/pve/qemu-server/' + group.id + '.conf', '/etc/pve/qemu-server/' + group.id + '.conf.backup'])
            #if rc != 0:
            #    print (stdout)
            #    print (stderr)
            #    continue

            rc, stdout, stderr, pid = execute_command(['scp', '-B', 'root@' + args.hostname + ':' + args.config_path + '/' + group.get_last_config(), '/etc/pve/qemu-server/' + group.id + '.conf'])
            if rc != 0:
                print (stdout)
                print (stderr)
                execute_command(['mv', '/etc/pve/qemu-server/' + group.id + '.conf.backup', '/etc/pve/qemu-server/' + group.id + '.conf'])
                continue
        no_restore_count = 0


        ###### Start the restore progress
        for disk in group.backed_up_disks:
            ### If the disk is set to be restored as a whole from backup
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

                ### If a keyfile was specified, and the disk is/was encrypted load the key in
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
                    ### If the parent zfs-dataset of the disk is also encrypted, inherit the key from it
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

            ### Disk which are set to rollback, will just rollback the local disk to the snapshot which has the same timestamp as a restore disk
            elif disk.rollback:
                no_restore_count = no_restore_count + 1
                print ("VM/CT ID " + group.id + " - rolling back " + disk.destination + " to " + disk.last_snapshot.split('@')[1])
                rc, stdout, stderr, pid = execute_command(['zfs', 'rollback', '-r', disk.destination + '@' + disk.last_snapshot.split('@')[1]])
                if rc != 0:
                    print (stdout)
                    print (stderr)
                    continue

            ### Disk which are set to keep, will not alter any current data, but delete any newer snapshots than the timestamp of the restore disk
            elif disk.keep:
                no_restore_count = no_restore_count + 1
                print ("VM/CT ID " + group.id + " - destroying newer snapshots than " + disk.last_snapshot.split('@')[1] + " on " + disk.destination)
                destroy_newer_snapshots(args, disk.destination, disk.last_snapshot)



        ###### Unlock for recreating #####
        if group.type == "lxc":
            execute_command(['pct', 'unlock', group.id])
        elif group.type == "qemu":
            execute_command(['qm', 'unlock', group.id])

        ###### Recreate disk if it was ot backed up and set to recreate #####
        for non_backed_up_disk in group.non_backed_up_disks:
            if non_backed_up_disk.recreate:
                print ("VM/CT ID " + group.id + " - Recreating " + non_backed_up_disk.unique_name)
                #config line: "mp0: vmsys:subvol-100-disk-1,mp=/test,backup=1,size=8G"
                #options: "mp=/test,backup=1,size=8G" as list
                options = non_backed_up_disk.config_line.split(',',1)[1].split(',')
                #hardware_id: mp1, scsi1, sata1 etc
                hardware_id = non_backed_up_disk.config_line.split(':', 1)[0]
                #from "size=8G" to "8"
                size = re.sub('[a-zA-Z]', '', [element for element in options if 'size=' in element][0].split('=')[1])
                #storage pool:from unique_name "vmsys:subvol-100-disk-1" to "vmsys"

                storage_pool = non_backed_up_disk.unique_name.split(':')[0]
                command = ""
                if group.type == "lxc": command = "pct"
                if group.type == "qemu": command = "qm"
                rc, stdout, stderr, pid = execute_command([command, 'set', group.id, f'--{hardware_id}', f"{storage_pool}:{size},{','.join(options)}"])
                if rc != 0:
                    print (stdout)
                    print (stderr)
                    continue

        ###### Lock again for snapshot cleanup #####
        if group.type == "lxc":
            execute_command(['pct', 'set', group.id, '--lock=backup'])
        elif group.type == "qemu":
            execute_command(['qm', 'set', group.id, '--lock=backup'])


        ###### Remove references to non existing disk snapshots in config #####
        print ("VM/CT ID " + group.id + " - Checking snapshot consistency, this may take a while.")
        cleanup_disks = group.backed_up_disks + group.non_backed_up_disks
        snaps_in_config = ""
        config = []
        if group.type == "lxc":
            snaps_in_config = execute_readonly_command(['pct', 'listsnapshot', group.id])[1]

            with open('/etc/pve/lxc/' + group.id + '.conf', 'r') as config_file:
                config = config_file.readlines()

        elif group.type == "qemu":
            snaps_in_config = execute_readonly_command(['qm', 'listsnapshot', group.id])[1]

            with open('/etc/pve/qemu-server/' + group.id + '.conf', 'r') as config_file:
                config = config_file.readlines()

        snaps_in_config = snaps_in_config.split('\n')

        for x in set(snaps_in_config).intersection(pzm_common.considered_empty):
            snaps_in_config.remove(x)

        snapnames_in_config = []
        for snap_in_config in snaps_in_config:
            #"             -> autoMonthly_2021-09-01_00-40-02 2021-09-01 00:40:02   "
            snapnames_in_config.append(snap_in_config.lstrip().split(' ')[1])
        if "current" in snapnames_in_config:
            snapnames_in_config.pop(snapnames_in_config.index("current"))

        config_new = config.copy() #Copy to have to have the reference
        for disk in cleanup_disks:
            print ("VM/CT ID " + group.id + " - Checking " + disk.unique_name)
            rc, stdout, stderr = execute_readonly_command(['zfs', 'list', '-t', 'snapshot', '-H', '-o', 'name', disk.destination])
            snapshots_on_disk = stdout.split('\n')
            for x in set(snapshots_on_disk).intersection(pzm_common.considered_empty):
                snapshots_on_disk.remove(x)

            #We only want the snapshot name
            snapshots_on_disk = [element.split('@')[1] for element in snapshots_on_disk]

            for snapname_in_config in snapnames_in_config:
                if snapname_in_config in snapshots_on_disk: #If a snapshot which is in the config is also on disk
                    continue #Then it's okay
                else: #If it's in the config, but not on disk
                    command = ""
                    if group.type == "lxc": command = "pct"
                    if group.type == "qemu": command = "qm"

                    rc, stdout, stderr = execute_readonly_command([command, 'config', group.id, '--snapshot', snapname_in_config])
                    if disk.unique_name in stdout:
                        if not pzm_common.test:
                            if pzm_common.debug: print ("VM/CT ID " + group.id + " - Deleting reference of " + disk.unique_name + " in snapshot " + snapname_in_config)
                            #Delete this snapshot from the config
                            #Read all lines from config, search for the snapshot name, delete the whole line where disk.unique_name is found at the next occourance
                            found_snapshot = False
                            config_tmp = []
                            for line in config_new:
                                if snapname_in_config in line and not "parent" in line: #Found snapshot header. Can only occour once in config file
                                    found_snapshot = True
                                if found_snapshot and disk.unique_name in line:
                                    #the snapshot was found previously and the line matches, skip writing that line, and set found_snapshot to False again
                                    #so it doesn't skip further occourance
                                    found_snapshot = False
                                else:
                                    config_tmp.append(line)
                            config_new = config_tmp
                        else:
                            if pzm_common.debug: print ("VM/CT ID " + group.id + " - Would delete reference of " + disk.unique_name + " in snapshot " + snapname_in_config)
                    else:
                        if pzm_common.debug: print ("VM/CT ID " + group.id + " - Disk " + disk.unique_name + " - snapshot " + snapname_in_config + " is OK!")

        if len(config) != len(config_new): #Config must have changed, if string list isn't of the same length anymore
            print ("VM/CT ID " + group.id + " - Snapshots found in config which do not exist on disk, deleting them from config.")
            if not pzm_common.test:
                if pzm_common.debug: print ("VM/CT ID " + group.id + " - Writing new config file for " + group.id + ", as file has changed by " + str(len(config)-len(config_new)) + " lines.")
                if group.type == "lxc":
                    with open('/etc/pve/lxc/' + group.id + '.conf', 'w') as config_file:
                        config_file.writelines(config_new)
            else:
                if pzm_common.debug: print ("VM/CT ID " + group.id + " - Would write new config file for " + group.id + ", as file has changed by " + str(len(config)-len(config_new)) + " lines.")
        else:
            print ("VM/CT ID " + group.id + " - Snapshots of config all exist on disk, nothing to cleanup")

        #Unlock before next group
        if group.type == "lxc":
            execute_command(['pct', 'unlock', group.id])
        elif group.type == "qemu":
            execute_command(['qm', 'unlock', group.id])

        print ("VM/CT ID " + group.id + " finished!")
    unlock(args.hostname)
