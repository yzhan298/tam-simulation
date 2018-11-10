import ConfigParser
import os
import random
import sys

import simpy

from hbase_config import ConfigError
from util.schedule_util import ScheduleUtil

# add parent directory to import path
sys.path.append(os.path.dirname(os.path.dirname((os.path.abspath(__file__)))))

from client import Client
from scheduler import FIFOScheduler
from stage import Stage, OnDemandStage
from stage_req import StageReq
from seda_resource import Resource


class DataNode(object):
    """
    Abstract class that represent an HDFS DataNode
    """

    DN_PER_REQ_CPU_TIME = 50e-6
    ACK_SIZE_RATIO = 48 / 64e3
    ACK_CPU_RATIO = 50e-6 / 64e3  # 50 us for every 64KB of data on vcpu

    def get_read_req(self, client_id, read_size):
        """

        :param client_id:
        :param read_size:
        :return: (req, done), where req is a StageReq that represents reading read_size in this datanode, and done is a
                 simpy event that indicates the read is completed
        """

        raise NotImplementedError()

    def get_write_req(self, client_id, write_size, downstream_dn_list):
        """

        :param client_id:
        :param write_size:
        :param downstream_dn_list: in addition to writing data to its own disk,  datanode will also pass the data to the
                                   datanodes in downstream_dn_list to be written, and the write is only considered
                                   completed when both local write and the writes in all downstream datanodes have
                                   complated.
        :return: (req, done), where req is a StageReq that represents writing write_size in this datanode, and done is a
                 simpy event that indicates the write is completed
        """

        raise NotImplementedError

    def get_short_circuit_req(self, client_id, read_size):
        """
        short circuit request incurs io resource cost but no other cost

        :param client_id:
        :param read_size:
        :return: (req, done), where req is a StageReq that represents a short-circuited read with read_size in this
                 datanode, and done is a simpy event that indicates the read is completed
        """
        raise NotImplementedError()

    @staticmethod
    def get_datanode(env, name, phy_node, datanode_conf, stage_log=sys.stdout):
        """

        :param env: simpy simulation environment
        :param name: datanode name (will be included in the log generated)
        :param datanode_conf: DataNodeConf object
        :param resource_log: an opened file object, the log generated by resources in this datanode will be written to
                             this file
        :param stage_log: an opened file object, the log generated by stages in this datanode will be written to this
                          file
        :return: DataNode instance
        """

        if datanode_conf.datanode_type == "simple":
            return SimpleDataNode.get_datanode(env, name, phy_node, datanode_conf, stage_log)
        else:
            raise ConfigError("Unsupported datanode type: " + datanode_conf.datanode_type)


class SimpleDataNode(DataNode):
    """
    Datanode the exhibit default HDFS DataNode behavior, with a Xceive stage (to perform read/write) and a PacketAck
    stage (to acknowledge written data packets)
    """

    def __init__(self, env, phy_node, xceive_stage, packet_ack_stage):
        """

        :param env:
        :param phy_node: the physical node this datanode runs on, datanode will use its CPU, I/O and network resources
        :param xceive_stage: stage that performs HDFS block I/O
        :param packet_ack_stage: stage that acknowledge written data packets
        """

        self.env = env
        self.phy_node = phy_node
        self.xceive_stage = xceive_stage
        self.packet_ack_stage = packet_ack_stage

    def get_read_req(self, client_id, read_size):
        xceive_req = StageReq(self.env, self.xceive_stage, client_id,
                              {self.phy_node.io_res: read_size, self.phy_node.net_res: read_size,
                               self.phy_node.cpu_res: DataNode.DN_PER_REQ_CPU_TIME},
                              [], [])
        return xceive_req, xceive_req.done

    def get_short_circuit_req(self, client_id, read_size):
        xceive_req = StageReq(self.env, self.xceive_stage, client_id,
                              {self.phy_node.io_res: read_size, self.phy_node.net_res: 0, self.phy_node.cpu_res: 0},
                              [], [])
        return xceive_req, xceive_req.done

    def get_write_req(self, client_id, write_size, downstream_dn_list):
        """
        Note: here we simplify a bit
        in real hdfs system, writes are divided into multiple packets
        and datanode flush packets to the downstream datanodes before it writes it locally
        but here we treat write as a single req (only ack once), and does local write before downstream pushing
        """

        xceive_req = StageReq(self.env, self.xceive_stage, client_id,
                              {self.phy_node.io_res: write_size, self.phy_node.net_res: write_size,
                               self.phy_node.cpu_res: DataNode.DN_PER_REQ_CPU_TIME}, [], [])
        packet_ack_req = StageReq(self.env, self.packet_ack_stage, client_id,
                                  {self.phy_node.net_res: write_size * DataNode.ACK_SIZE_RATIO,
                                   self.phy_node.cpu_res: write_size * DataNode.ACK_CPU_RATIO},
                                  [], [])
        xceive_req.downstream_reqs.append(packet_ack_req)

        if len(downstream_dn_list) != 0:
            downstream_dn = downstream_dn_list[0]
            d_xceive_req, d_packet_ack_done = downstream_dn.get_write_req(client_id, write_size, [] if len(
                downstream_dn_list) == 1 else downstream_dn_list[1:])
            xceive_req.downstream_reqs.append(d_xceive_req)
            packet_ack_req.blocking_evts.append(d_packet_ack_done)

        return xceive_req, packet_ack_req.done

    @staticmethod
    def get_datanode(env, name, phy_node, datanode_conf, stage_log=sys.stdout):
        if datanode_conf.datanode_xceive_stage_scheduler_generator is None:
            xceive_stage = OnDemandStage(env, name + "_xceive", log_file=stage_log)
        else:
            xceive_scheduler = datanode_conf.datanode_xceive_stage_scheduler_generator(env)
            xceive_cost_func = ScheduleUtil.get_cost_func(datanode_conf.datanode_xceive_stage_schedule_resource,
                                                          phy_node)
            xceive_stage = Stage(env, name + "_xceive",
                                 datanode_conf.datanode_xceive_stage_handlers,
                                 xceive_scheduler, xceive_cost_func, log_file=stage_log)

        if datanode_conf.datanode_packet_ack_stage_scheduler_generator is None:
            packet_ack_stage = OnDemandStage(env, name + "_packet_ack", log_file=stage_log)
        else:
            packet_ack_scheduler = datanode_conf.datanode_packet_ack_stage_scheduler_generator(env)
            packet_ack_cost_func = ScheduleUtil.get_cost_func(datanode_conf.datanode_packet_ack_stage_schedule_resource,
                                                              phy_node)
            packet_ack_stage = Stage(env, name + "_packet_ack", datanode_conf.datanode_packet_responders,
                                     packet_ack_scheduler, packet_ack_cost_func, log_file=stage_log)

        xceive_stage.monitor_interval = datanode_conf.stage_monitor_interval
        packet_ack_stage.monitor_interval = datanode_conf.stage_monitor_interval
        datanode = SimpleDataNode(env, phy_node, xceive_stage, packet_ack_stage)

        return datanode


class NameNode(object):
    """
    Representing an HDFS NameNode

    In Namenode we omit rpc_reader and rpc_responder stage since we are only interested in the namespace lock resource,
    which is consumed in the rpc_process_stage
    """

    def __init__(self, env, phy_node, rpc_process_stage, lock_res):
        """
        :param env:
        :param phy_node: physical node this namenode runs on
        :param rpc_process_stage:
        :param lock_res: hdfs namespace lock
        """
        self.env = env
        self.phy_node = phy_node
        self.rpc_process_stage = rpc_process_stage
        self.lock_res = lock_res

    def get_metadata_req(self, client_id, lock_time):
        """

        :param client_id:
        :param lock_time:  time spent holding the namespace lock
        :return: (req, done), where req is a StageReq that represents looking up (or modifying) the namespace metadata,
                done is a simpy event indicating the completion of the matadata operation
        """
        metadata_req = StageReq(self.env, self.rpc_process_stage, client_id, {self.lock_res: lock_time}, [], [])
        return metadata_req, metadata_req.done


class HDFS(object):

    def __init__(self, env, namenode, datanode_list, replica=3):
        """
        :param env: simpy simulation environment
        :param namenode: HDFS NamdeNode
        :param datanode_list: A list of DataNode
        :param replica: how may replicas of data are written during an HDFS write, defaults to 3
        """

        self.env = env
        self.namenode = namenode
        self.datanode_list = datanode_list
        self.replica = replica

    def __str__(self):
        return "HDFS with namenode: " + str(self.namenode) + " datanode_list: " + str(self.datanode_list)

    def get_read_req(self, client_id, read_size):
        """
        get read request from a random datanode in the datanode_list

        :param client_id:
        :param read_size:
        :return: (req, done), where req  is a StageReq that represents reading from one of the HDFS Datanodes,
                 done is the read completion event
        """
        datanode = self.datanode_list[random.randrange(len(self.datanode_list))]
        return datanode.get_read_req(client_id, read_size)

    def get_write_req(self, phy_node, client_id, write_size):
        """

        :param phy_node: the physical node where the write is initiated; if one of the datanodes run on phy_node, HDFS
                         will write one replica to that datanode.
        :param client_id:
        :param write_size:
        :return: (req, done), where req is a StageReq that represents writes to the HDFS (internally HDFS will write
                 multiple replicas to a random set of datanodes), done is a simpy event indicating the completion of the
                 write
        """

        assert self.replica <= len(self.datanode_list)
        datanode_list = []

        local_datanode = None
        for node in self.datanode_list:
            if node.phy_node == phy_node:
                local_datanode = node
                break

        if local_datanode is not None:
            datanode_list.append(local_datanode)

        # we are guaranteed to find enough unique datanodes because we have enough datanodes (>=replica)
        while len(datanode_list) < self.replica:
            datanode = self.datanode_list[random.randrange(len(self.datanode_list))]
            if datanode in datanode_list:
                continue
            datanode_list.append(datanode)

        xceive_req, ack_done = datanode_list[0].get_write_req(client_id, write_size,
                                                              [] if len(datanode_list) == 1 else datanode_list[1:])
        return xceive_req, ack_done

    def get_short_circuit_req(self, phy_node, client_id, read_size):
        """
        get short circuit request where datanode is co-located in the phy_node

        :param phy_node: the physical node where the read is initiated
        :param client_id:
        :param read_size:
        :return: (req, done)
        """
        datanode = None
        for node in self.datanode_list:
            if node.phy_node == phy_node:
                datanode = node
                break

        if datanode is None:
            raise RuntimeError("phy_node not found in the hdfs datanode_list!")

        return datanode.get_short_circuit_req(client_id, read_size)

    def get_namenode_req(self, client_id, lookup_time):
        """
        :param client_id:
        :param lookup_time: time spent looking up the namespace (thus holding the namespace lock)
        :return: (req, done)
        """
        return self.namenode.get_metadata_req(client_id, lookup_time)

    @staticmethod
    def get_cluster(env, phy_cluster, hdfs_conf, resource_log=sys.stdout, stage_log=sys.stdout):
        """

        :param env:
        :param phy_cluster: the PhysicalCluster HDFS run on; the first node will run NameNode; other nodes will run
                            DataNode
        :param hdfs_conf: HDFSConf object
        :param resource_log: opened file handle
        :param stage_log: opened file handle
        :return: newly constructed HDFS object
        """
        assert len(phy_cluster.node_list) >= 2

        namespace_lock_res = Resource(env, "namespace_lock", 1, 1, FIFOScheduler(env, float('inf')),
                                      log_file=resource_log)
        if hdfs_conf.namenode_scheduler_generator is None:
            namenode_rpc_stage = OnDemandStage(env, "namenode_rpc", log_file=stage_log)
        else:
            namenode_scheduler = hdfs_conf.namenode_scheduler_generator(env)
            namenode_rpc_stage = Stage(env, "namenode_rpc", hdfs_conf.namenode_handlers,
                                       namenode_scheduler, log_file=stage_log)

        namenode = NameNode(env, phy_cluster.node_list[0], namenode_rpc_stage, namespace_lock_res)

        datanode_list = []
        for phy_node in phy_cluster.node_list[1:]:
            datanode = DataNode.get_datanode(env, "datanode_" + str(len(datanode_list)), phy_node,
                                             hdfs_conf.datanode_conf, stage_log)
            datanode_list.append(datanode)

        hdfs = HDFS(env, namenode, datanode_list, hdfs_conf.replica)

        return hdfs


class HDFSClient(Client):
    """
    HDFSClient continues issue HDFS requests and wait on their completions (i.e., a closed loop client)
    The requests contain three parts, and the client perform the three parts sequentially
    1. look up the hbase name space (controlled by lookup time)
    2. read some data from HDFS (controlled by read_size)
    3. write some data to HDFS (controlled by write_size)

    One can set lookup_time, read_size, or write_size to 0, in which case client will skip the corresponding parts
    """

    # noinspection PyMissingConstructor
    def __init__(self, env, hdfs, name, client_id, num_instances, lookup_time, read_size, write_size, think_time,
                 log_file=sys.stdout):
        """

        :param env:
        :param hdfs:
        :param name:  client name
        :param client_id: scheduling is based on client id
        :param num_instances: number of instances that continunously issue requests
        :param lookup_time:
        :param read_size:
        :param write_size:
        :param think_time: after a request is completed, client will wait for think_time before issuing a new request
        :param log_file: open file handle, client log will be written to it
        """
        self.env = env
        self.hdfs = hdfs
        self.client_name = name
        self.client_id = client_id
        self.lookup_time = lookup_time
        self.read_size = read_size
        self.write_size = write_size
        self.think_time = think_time
        self.log_file = log_file
        self.instances = []
        for i in range(num_instances):
            self.instances.append(env.process(self.run()))
        self.monitor_interval = 10
        self.monitor = env.process(self.monitor_run())

        # ===statistics=====
        self.num_reqs = 0
        self.total_latency = 0
        self.max_latency = 0
        self.last_report = self.env.now

    def run(self):
        while True:
            start = self.env.now

            # First look up the metadata
            if self.lookup_time > 0:
                lookup_req = self.hdfs.get_namenode_req(self.client_id, self.lookup_time)
                yield lookup_req.submit()
                lookup_done = simpy.AllOf(self.env, lookup_req.all_done_list())
                yield lookup_done

            # Then read the data
            if self.read_size > 0:
                read_req, done_evt = self.hdfs.get_read_req(self.client_id, self.read_size)
                yield read_req.submit()
                yield done_evt

            # Then write
            if self.write_size > 0:
                write_req, ack_done = self.hdfs.get_write_req(None, self.client_id, self.write_size)
                yield write_req.submit()
                yield ack_done

            latency = self.env.now - start
            self.num_reqs += 1  # a lookup + a read + a write counts as one req
            self.total_latency += latency

            if self.think_time != 0:
                yield self.env.timeout(self.think_time)

    @staticmethod
    def get_clients(env, hdfs, config, client_log_file=sys.stdout):
        """
        :param env:
        :param hdfs: the hdfs cluster that the clients issue requests to
        :param config: ConfigParser object
        :param client_log_file: file handler which clients write statistics to
        :return: a list of clients specified in the config_file
        """
        client_list = []

        for section in config.sections():
            if "hdfs_client" not in section:
                continue

            try:
                client_id = config.getint(section, "client_id")
                client_name = config.get(section, "client_name")
                num_instances = config.getint(section, "num_instances")
                lookup_time = config.getfloat(section, "lookup_time")
                read_size = config.getfloat(section, "read_size")
                write_size = config.getfloat(section, "write_size")
                think_time = config.getfloat(section, "think_time")
                monitor_interval = 10
                if "monitor_interval" in config.options(section):
                    monitor_interval = config.getfloat(section, "monitor_interval")
            except ConfigParser.NoOptionError as error:
                raise ConfigError(
                    "client " + error.section + " configuration not complete, missing option: " + error.option)

            client = HDFSClient(env, hdfs, client_name, client_id, num_instances, lookup_time, read_size, write_size,
                                think_time, log_file=client_log_file)
            client.monitor_interval = monitor_interval
            client_list.append(client)

        if len(client_list) == 0:
            print "Warning: no hdfs_client configuration found in", config, "returning an empty list"

        return client_list