import schedule, time, threading, settings, re, os, sys
from tools import ossTools, sshTools, get_file_md5, zip_dir, aliEcsSnapshot
from os import listdir
from datetime import datetime

UPLOADING = 0
TIMESTAMP = int(time.time())
CURSOR = TIMESTAMP


def get_cursor():
    with open(settings.cur_file, 'r') as f:
        cursor = f.read()
        if not cursor.isdigit():
            pass
            # logger
            exit(404)
        else:
            return int(cursor)


def put_cursor(cursor):
    with open(settings.cur_file, 'r+') as f:
        f.write(str(cursor))


def oplog_dump():
    cursor = get_cursor()
    start_time = cursor
    end_time = cursor + settings.dumpop_interval
    for db_info in settings.dbs_info:
        host = db_info.get('host')
        ip = db_info.get('ip')
        port = db_info.get('port')
        local_store = settings.local_store
        temp_dir_name = '{local_store}{db_name}-{port}_{start_time}_{end_time}_temp'.format(local_store=local_store,
                                                                                            db_name=host, port=port,
                                                                                            start_time=start_time,
                                                                                            end_time=end_time)
        dir_name = '{local_store}{db_name}-{port}_{start_time}_{end_time}'.format(local_store=local_store, db_name=host,
                                                                                  port=port, start_time=start_time,
                                                                                  end_time=end_time)
        cmd = '''mongodump --port {port} -u {user} -p {password} --authenticationDatabase={auth_db} -d local -c oplog.rs -q '{{"ts": {{$gt:Timestamp({start_time}, 1),$lt:Timestamp({end_time}, 1)}}}}' -o {file_name} '''.format(
            host=host, ip=ip, port=port, user=settings.db_user, password=settings.db_password, auth_db=settings.auth_db,
            start_time=start_time, end_time=end_time, file_name=temp_dir_name)
        job_thread = threading.Thread(target=op_dump_exec, args=(ip, cmd, dir_name, temp_dir_name))
        job_thread.start()

    put_cursor(end_time)


def op_dump_exec(ip, cmd, dir_name, temp_dir_name):
    ssh = sshTools(ip)
    status, out, err = ssh.execute_cmd(cmd)
    if status == 0:
        status, out, err = ssh.execute_cmd(
            'mv {temp_dir_name} {dir_name}'.format(temp_dir_name=temp_dir_name, dir_name=dir_name))
        if status != 0:
            pass
            # logger
    else:
        pass
        # logger
    ssh.ssh.close()


def oas_upload():
    global UPLOADING
    if UPLOADING == 1:
        pass
    else:
        UPLOADING = 1
        dir_list = listdir(settings.local_store)
        for dir in dir_list:
            print(dir)
            res = re.match('^.*_\d{10}_\d{10}$', dir)
            if res:
                match_obj = re.match('([a-zA-Z-0-9]+).*_\d{10}_\d{10}', dir)
                if not match_obj:
                    print('not match')
                    continue
                    # logger
                else:
                    host = match_obj.group(1)

                # 压缩local目录以及其下面的文件
                zip_dir(settings.local_store + dir + '/local', settings.local_store + dir + '/local.zip')
                print('压缩完毕')

                # 计算文件的md5值并输出到文件中
                file_md5 = get_file_md5(settings.local_store + dir + '/local.zip')
                with open(settings.local_store + dir + '/local.md5', 'w') as md5_f:
                    md5_f.write(file_md5)

                oss = ossTools(settings.access_key_id, settings.access_key_secret)
                remote_file = datetime.now().strftime('%Y/%m/%d/') + host + '/' + dir
                local_data_file = settings.local_store + dir + '/local.zip'
                res_up_data = oss.multi_upload_obj(remote_file + '/local.zip', local_data_file)
                local_md5_file = settings.local_store + dir + '/local.md5'
                res_up_md5 = oss.multi_upload_obj(remote_file + '/local.md5', local_md5_file)

                if not res_up_data and not res_up_md5:
                    try:
                        for restore_file in listdir(settings.local_store + dir + '/local/'):
                            os.remove(settings.local_store + dir + '/local/' + restore_file)
                    except:
                        pass
                    try:
                        os.rmdir(settings.local_store + dir + '/local')
                    except:
                        pass
                    try:
                        for restore_file in listdir(settings.local_store + dir):
                            os.remove(settings.local_store + dir + '/' + restore_file)
                    except:
                        pass
                    try:
                        os.rmdir(settings.local_store + dir)
                    except:
                        pass

        UPLOADING = 0


def full_db_backup():
    # 一次只做一个host的，相当于抛出一个线程去维护一个host的所有db的全量备份，也就是备份这个host的所有磁盘，
    wholebak_infos = get_wholebak_infos()
    for host, info in wholebak_infos.items():
        job_thread = threading.Thread(target=make_full_backup, args=(host, info))
        job_thread.start()


def make_full_backup(host, info):
    ali_ecs_snap = aliEcsSnapshot(settings.access_key_id, settings.access_key_secret, settings.region_id)
    # 锁表
    ip = info.get('ip')
    port_lst = info.get('port')
    ssh = sshTools(ip)
    tags = []
    des_info = ''
    for port in port_lst:
        lock_time = int(time.time())
        ssh.execute_cmd(
            "echo 'db.runCommand({{fsync:1,lock:1}});' | mongo --port {port} admin -u {user} -p {password} ".format(
                port=port, user=settings.db_user, password=settings.db_password))
        # 检查是否锁，没有则抛错，继续锁，锁三次, 再失败则放弃，报错

        # 取timestamp
        start_timestamp_dic = {}
        status, start_timestamp, err = ssh.execute_cmd(
            '''
            echo \'\'\'rs.slaveOk()
                use local
                db.replset.minvalid.find({},{_id:0,begin:1})
                \'\'\' | mongo --port %d admin -u %s -p %s --quiet | grep begin | awk -F '[(,)]' '{print $2}'
            ''' % (port, settings.db_user, settings.db_password))
        start_timestamp = start_timestamp[0].strip()
        status, start_timestamp_cur, err = ssh.execute_cmd(
            '''
            echo \'\'\'rs.slaveOk()
                use local
                db.replset.minvalid.find({},{_id:0,begin:1})
                \'\'\' | mongo --port %d admin -u %s -p %s --quiet | grep begin |awk -F '[(,)]' '{print $3}'
            ''' % (port, settings.db_user, settings.db_password))[0].strip()
        start_timestamp_cur = start_timestamp_cur[0].strip()
        if not start_timestamp:
            start_timestamp = lock_time
        start_timestamp_dic[port] = start_timestamp
        tags.append({'Key': '%s_%s' % (host, port),
                     'Value': '{"%s":{"%s":["%s","%s"]}}' % (host, port, start_timestamp, start_timestamp_cur)})
        des_info += str(port) + 'at' + str(start_timestamp) + ' '
    # 打快照
    instance_id = ali_ecs_snap.get_instanceid([ip])
    disk_ids = ali_ecs_snap.get_disk_ids(instance_id)
    for disk_id in disk_ids:
        snap_name = '{host}-{start_timestamp}'.format(host=host, start_timestamp=datetime.now().strftime('%Y-%m-%d'))
        description = 'mongo full backup' + des_info
        snap_response = ali_ecs_snap.create_snapshot(disk_id, snap_name, tags, description)
        print(snap_response)

    # 解锁实例 # 整备与增量备份连接
    inc_bak_cursor = get_cursor()
    for port in port_lst:
        ssh.execute_cmd("echo 'db.fsyncUnlock();' | mongo --port {port} admin -u {user} -p {password}".format(port=port,
                                                                                                              user=settings.db_user,
                                                                                                              password=settings.db_password))
        full_bak_time = start_timestamp_dic[port]
        if abs(int(inc_bak_cursor) - int(full_bak_time)) >= 43200:
            print('-----> to old time')
        else:
            print('-----> begin to link ful and inc')
            if int(inc_bak_cursor) > int(full_bak_time):
                start_time = full_bak_time
                end_time = inc_bak_cursor
            elif int(inc_bak_cursor) < int(full_bak_time):
                start_time = full_bak_time
                end_time = inc_bak_cursor + settings.dumpop_interval
            else:
                start_time = inc_bak_cursor
                end_time = inc_bak_cursor - 1

            local_store = settings.local_store
            temp_dir_name = '{local_store}{db_name}-{port}_forfull_{start_time}_{end_time}_temp'.format(
                local_store=local_store,
                db_name=host, port=port,
                start_time=start_time,
                end_time=end_time)
            dir_name = '{local_store}{db_name}-{port}_forfull_{start_time}_{end_time}'.format(local_store=local_store,
                                                                                              db_name=host,
                                                                                              port=port,
                                                                                              start_time=start_time,
                                                                                              end_time=end_time)
            cmd = '''mongodump --port {port} -u {user} -p {password} --authenticationDatabase={auth_db} -d local -c oplog.rs -q '{{"ts": {{$gt:Timestamp({start_time}, 1),$lt:Timestamp({end_time}, 1)}}}}' -o {file_name} '''.format(
                host=host, ip=ip, port=port, user=settings.db_user, password=settings.db_password,
                auth_db=settings.auth_db, start_time=start_time, end_time=end_time, file_name=temp_dir_name)
            op_dump_exec(ip, cmd, dir_name, temp_dir_name)


def get_wholebak_infos():
    wholebak_infos = {}
    for db_info in settings.dbs_info:
        if db_info.get('host') not in wholebak_infos.keys():
            wholebak_infos[db_info.get('host')] = {'ip': db_info.get('ip'), 'port': [db_info.get('port')]}
        else:
            wholebak_infos[db_info.get('host')]['port'].append(db_info.get('port'))
    return wholebak_infos


def logger():
    pass


def main():
    if os.path.exists(settings.cur_file):
        print('exits')
        with open(settings.cur_file, 'r') as f:
            cursor = f.read()
        if not cursor.isdigit() or not re.match('^\d{10}$', cursor):
            # logger
            print('not digit')
            with open(settings.cur_file, 'w') as f:
                cursor = int(time.time())
                f.write(str(cursor))

    else:
        print('new')
        with open(settings.cur_file, 'w') as f:
            cursor = int(time.time())
            f.write(str(cursor))

    schedule.every(settings.dumpop_interval).seconds.do(oplog_dump)
    schedule.every(settings.upload_interval).seconds.do(oas_upload)
    ##########schedule.every().day.at(settings.full_bakdb_at).do(full_db_backup)
    while True:
        schedule.run_pending()
        time.sleep(settings.sche_sleep)


if __name__ == '__main__':
    # schedule.every(settings.dumpop_interval).seconds.do(oplog_dump)
    # schedule.every(settings.upload_interval).seconds.do(oas_upload)
    #
    # schedule.run_all()
    # while True:
    #     print("======check schedule======")
    #     sys.stdout.write("======check schedule======")
    #     schedule.run_pending()
    #     time.sleep(settings.sche_sleep)

    pass
