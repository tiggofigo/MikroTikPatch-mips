import subprocess,lzma
import struct,os
from npk import NovaPackage,NpkPartID,NpkFileContainer

def patch_bzimage(data:bytes,key_dict:dict):
    PE_TEXT_SECTION_OFFSET = 414
    HEADER_PAYLOAD_OFFSET = 584
    HEADER_PAYLOAD_LENGTH_OFFSET = HEADER_PAYLOAD_OFFSET + 4
    text_section_raw_data = struct.unpack_from('<I',data,PE_TEXT_SECTION_OFFSET)[0]
    payload_offset =  text_section_raw_data +struct.unpack_from('<I',data,HEADER_PAYLOAD_OFFSET)[0]
    payload_length = struct.unpack_from('<I',data,HEADER_PAYLOAD_LENGTH_OFFSET)[0]
    payload_length = payload_length - 4 #last 4 bytes is uncompressed size(z_output_len)
    z_output_len = struct.unpack_from('<I',data,payload_offset+payload_length)[0]
    vmlinux_xz = data[payload_offset:payload_offset+payload_length]
    vmlinux = lzma.decompress(vmlinux_xz)
    assert z_output_len == len(vmlinux), 'vmlinux size is not equal to expected'
    CPIO_HEADER_MAGIC = b'07070100'
    CPIO_FOOTER_MAGIC = b'TRAILER!!!\x00\x00\x00\x00' #545241494C455221212100000000
    cpio_offset1 = vmlinux.index(CPIO_HEADER_MAGIC)
    initramfs = vmlinux[cpio_offset1:]
    cpio_offset2 = initramfs.index(CPIO_FOOTER_MAGIC)+len(CPIO_FOOTER_MAGIC)
    initramfs = initramfs[:cpio_offset2]
    new_initramfs = initramfs       
    for old_public_key,new_public_key in key_dict.items():
        if old_public_key in new_initramfs:
            print(f'initramfs public key patched {old_public_key[:16].hex().upper()}...')
            new_initramfs = new_initramfs.replace(old_public_key,new_public_key)
    new_vmlinux = vmlinux.replace(initramfs,new_initramfs)
    new_vmlinux_xz = lzma.compress(new_vmlinux,check=lzma.CHECK_CRC32,filters=[
            {"id": lzma.FILTER_X86},
            {"id": lzma.FILTER_LZMA2, "preset": 8,'dict_size': 32*1024*1024},
        ])
    new_payload_length = len(new_vmlinux_xz)
    assert new_payload_length <= payload_length , 'new vmlinux.xz size is too big'
    new_payload_length = new_payload_length + 4 #last 4 bytes is uncompressed size(z_output_len)
    new_data = bytearray(data)
    struct.pack_into('<I',new_data,HEADER_PAYLOAD_LENGTH_OFFSET,new_payload_length)
    vmlinux_xz += struct.pack('<I',z_output_len)
    new_vmlinux_xz += struct.pack('<I',z_output_len)
    new_vmlinux_xz = new_vmlinux_xz.ljust(len(vmlinux_xz),b'\0')
    new_data = new_data.replace(vmlinux_xz,new_vmlinux_xz)
    return new_data

def run_shell_command(command):
    process = subprocess.run(command, shell=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return process.stdout, process.stderr


def patch_bootloader(key_dict,boot_dev):
    IMAGE_OFFSET_BLK = 512
    BLOCK_SIZE = 4096
    #debugfs /dev/sda1 -R 'icheck 512' 2> /dev/null | grep 512   
    stdout,stderr = run_shell_command(f"debugfs {boot_dev} -R 'icheck {IMAGE_OFFSET_BLK}' 2> /dev/null | sed -n '2p'")
    tmp = stdout.decode().strip().split('\t')
    assert len(tmp) >= 2 , f'debugfs icheck error {tmp} {stderr.decode()}'
    inode = int(tmp[1])
    print(f'inode : {inode}')
    #sudo debugfs /dev/sda1 -R 'stat <12>' 2> /dev/null | sed -n '11p' 
    stdout,stderr = run_shell_command(f"debugfs {boot_dev} -R 'stat <12>' 2> /dev/null | sed -n '11p' ")
    blocks_info = stdout.decode().strip().split(',')
    blocks = []
    ind_block_id = None
    for block_info in blocks_info:
        _tmp = block_info.strip().split(':')
        if _tmp[0].strip() == '(IND)':
            ind_block_id =  int(_tmp[1])
        else:
            id_range = _tmp[0].strip().replace('(','').replace(')','').split('-')
            block_range = _tmp[1].strip().replace('(','').replace(')','').split('-')
            blocks += [id for id in range(int(block_range[0]),int(block_range[1])+1)]
    print(f' blocks : {len(blocks)} ind_block_id : {ind_block_id}')
    stdout,stderr = run_shell_command(f"debugfs {boot_dev} -R 'cat <{inode}>' 2> /dev/null")
    bzImage = stdout
    new_bzImage = patch_bzimage(bzImage,key_dict)
    print(f'write block {len(blocks)} : [',end="")
    with open(boot_dev,'wb') as f:
        for index,block_id in enumerate(blocks):
            print('#',end="")
            f.seek(block_id*BLOCK_SIZE)
            f.write(new_bzImage[index*BLOCK_SIZE:(index+1)*BLOCK_SIZE])
        f.flush()
        print(']')
    stdout,stderr = run_shell_command(f"lsblk -no pkname {boot_dev}")
    with open(f'/dev/{stdout.decode().strip()}','wb') as f:
        f.seek(0x150)
        f.write(b'\x00')
        f.flush()

def patch_squashfs(path,key_dict):
    for root, dirs, files in os.walk(path):
        for file in files:
            file = os.path.join(root,file)
            if os.path.isfile(file):
                data = open(file,'rb').read()
                for old_public_key,new_public_key in key_dict.items():
                    if old_public_key in data:
                        print(f'{file} public key patched {old_public_key[:16].hex().upper()}...')
                        data = data.replace(old_public_key,new_public_key)
                        open(file,'wb').write(data)

def patch_npk_file(key_dict,kcdsa_private_key,eddsa_private_key,input_file,output_file=None):
    npk = NovaPackage.load(input_file)    
    if npk[NpkPartID.NAME_INFO].data.name == 'system':
        file_container = NpkFileContainer.unserialize_from(npk[NpkPartID.FILE_CONTAINER].data)
        for item in file_container:
            if item.name == b'boot/EFI/BOOT/BOOTX64.EFI':
                print(f'patch {item.name} ...')
                item.data = patch_bzimage(item.data,key_dict)
                open('linux','wb').write(item.data)
            elif item.name == b'boot/kernel':
                from netinstall import patch_elf
                print(f'patch {item.name} ...')
                item.data = patch_elf(item.data,key_dict)
                open('linux','wb').write(item.data)
        npk[NpkPartID.FILE_CONTAINER].data = file_container.serialize()
        try:
            squashfs_file = 'squashfs.sfs'
            extract_dir = 'squashfs-root'
            open(squashfs_file,'wb').write(npk[NpkPartID.SQUASHFS].data)
            print(f"extract {squashfs_file} ...")
            _, stderr = run_shell_command(f"unsquashfs -d {extract_dir} {squashfs_file}")
            print(stderr.decode())
            patch_squashfs(extract_dir,key_dict)
            print(f"pack {extract_dir} ...")
            run_shell_command(f"rm -f {squashfs_file}")
            _, stderr = run_shell_command(f"mksquashfs {extract_dir} {squashfs_file} -quiet -comp xz -no-xattrs -b 256k")
            print(stderr.decode())
        except Exception as e:
            print(e)
        print(f"clean ...")
        run_shell_command(f"rm -rf {extract_dir}")
        npk[NpkPartID.SQUASHFS].data = open(squashfs_file,'rb').read()
        run_shell_command(f"rm -f {squashfs_file}")
    npk.sign(kcdsa_private_key,eddsa_private_key)
    npk.save(output_file or input_file)

if __name__ == '__main__':
    import argparse,os
    parser = argparse.ArgumentParser(description='MikroTik patcher')
    subparsers = parser.add_subparsers(dest="command")
    npk_parser = subparsers.add_parser('npk',help='patch and sign npk file')
    npk_parser.add_argument('input',type=str, help='Input file')
    npk_parser.add_argument('-o','--output',type=str,help='Output file')
    boot_parser = subparsers.add_parser('boot',help='patch bootloader')
    boot_parser.add_argument('dev',type=str, help='boot device')
    netinstall_parser = subparsers.add_parser('netinstall',help='patch netinstall file')
    netinstall_parser.add_argument('input',type=str, help='Input file')
    netinstall_parser.add_argument('-o','--output',type=str,help='Output file')
    args = parser.parse_args()
    key_dict = {
        bytes.fromhex(os.environ['MIKRO_LICENSE_PUBLIC_KEY']):bytes.fromhex(os.environ['CUSTOM_LICENSE_PUBLIC_KEY']),
        bytes.fromhex(os.environ['MIKRO_NPK_SIGN_PUBLIC_KEY']):bytes.fromhex(os.environ['CUSTOM_NPK_SIGN_PUBLIC_KEY'])
    }
    kcdsa_private_key = bytes.fromhex(os.environ['CUSTOM_LICENSE_PRIVATE_KEY'])
    eddsa_private_key = bytes.fromhex(os.environ['CUSTOM_NPK_SIGN_PRIVATE_KEY'])
    if args.command =='npk':
        print(f'patching {args.input} ...')
        patch_npk_file(key_dict,kcdsa_private_key,eddsa_private_key,args.input,args.output)
    elif args.command =='boot':
        print(f'patching {args.dev} ...')
        patch_bootloader(key_dict,args.dev)
    elif args.command == 'netinstall':
        from netinstall import patch_netinstall
        print(f'patching {args.input} ...')
        patch_netinstall(key_dict,args.input,args.output)
    else:
        parser.print_help()


    
