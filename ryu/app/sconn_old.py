from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types
from ryu.topology import event
from ryu.topology.api import get_switch, get_link

import networkx as nx
import logging

class SconnControllerV9(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(SconnControllerV9, self).__init__(*args, **kwargs)
        self.topology_api_app = self
        self.switch_net = nx.Graph()
        self.stp_net = nx.Graph()
        self.mac_to_port = {}
        self.datapaths = {}
        self.manual_link_added = False
        self.logger.setLevel(logging.INFO)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)
        self.logger.info("Switch %s connected.", datapath.id)

    def add_flow(self, datapath, priority, match, actions, buffer_id=None, idle=0, hard=0):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=datapath, buffer_id=buffer_id if buffer_id else ofproto.OFP_NO_BUFFER,
                                idle_timeout=idle, hard_timeout=hard,
                                priority=priority, match=match,
                                instructions=inst)
        datapath.send_msg(mod)

    def _calculate_stp(self):
        if self.switch_net.nodes:
            self.stp_net = nx.minimum_spanning_tree(self.switch_net)
            self.logger.info("Spanning Tree Links (used for flooding): %s", sorted(self.stp_net.edges()))

    @set_ev_cls([event.EventSwitchEnter, event.EventLinkAdd, event.EventLinkDelete])
    def topology_change_handler(self, ev):
        if isinstance(ev, event.EventSwitchEnter):
            switch = ev.switch
            self.switch_net.add_node(switch.dp.id)
            self.logger.info("Switch %s entered.", switch.dp.id)
            
            # Manually add the wireless mesh link once both APs are present
            if not self.manual_link_added and 2 in self.switch_net and 3 in self.switch_net:
                self.switch_net.add_edge(2, 3, port=6)
                self.switch_net.add_edge(3, 2, port=6)
                self.manual_link_added = True
                self.logger.info("Manually added wireless link between 2 and 3.")

        elif isinstance(ev, event.EventLinkAdd):
            link = ev.link
            self.switch_net.add_edge(link.src.dpid, link.dst.dpid, port=link.src.port_no)
            self.switch_net.add_edge(link.dst.dpid, link.src.dpid, port=link.dst.port_no)
            self.logger.info("Link added: %s <--> %s", link.src.dpid, link.dst.dpid)

        elif isinstance(ev, event.EventLinkDelete):
            link = ev.link
            # Check if the edge exists before removing to avoid errors
            if self.switch_net.has_edge(link.src.dpid, link.dst.dpid):
                self.switch_net.remove_edge(link.src.dpid, link.dst.dpid)
                self.switch_net.remove_edge(link.dst.dpid, link.src.dpid)
                self.logger.warning("Link deleted: %s <--> %s", link.src.dpid, link.dst.dpid)
                
                # Clear flows and MAC table ONLY when a link is confirmed to be deleted
                self.logger.warning("Clearing all flows and MAC table to force path re-learning.")
                for dp in self.datapaths.values():
                    ofproto = dp.ofproto
                    parser = dp.ofproto_parser
                    mod = parser.OFPFlowMod(dp, command=ofproto.OFPFC_DELETE,
                                            out_port=ofproto.OFPP_ANY, out_group=ofproto.OFPG_ANY,
                                            match=parser.OFPMatch())
                    dp.send_msg(mod)
                self.mac_to_port.clear()

        self._calculate_stp()
        self.logger.info("Current Full Links: %s", sorted(self.switch_net.edges()))
        # =======================================================

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        dst = eth.dst
        src = eth.src
        dpid = datapath.id
        self.mac_to_port.setdefault(dpid, {})

        if self.mac_to_port[dpid].get(src) != in_port:
            self.mac_to_port[dpid][src] = in_port
            self.logger.info("Learned/Updated MAC %s on switch %s port %s", src, dpid, in_port)

        if dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst]
            actions = [parser.OFPActionOutput(out_port)]
        else:
            self.logger.info("Destination %s unknown on switch %s. Flooding via Spanning Tree.", dst, dpid)
            
            all_ports = self.datapaths[dpid].ports.keys()
            inter_switch_ports = []
            if dpid in self.switch_net:
                for neighbor in self.switch_net.neighbors(dpid):
                    inter_switch_ports.append(self.switch_net[dpid][neighbor]['port'])

            flood_ports = []
            for p in all_ports:
                if p == in_port:
                    continue
                if p not in inter_switch_ports:
                    flood_ports.append(p)
                else:
                    if dpid in self.stp_net and self.stp_net.has_edge(dpid, self._get_neighbor_by_port(dpid, p)):
                        flood_ports.append(p)
            
            self.logger.info("Flooding on switch %s to ports: %s", dpid, flood_ports)
            actions = [parser.OFPActionOutput(p) for p in flood_ports]

        if len(actions) == 1 and actions[0].port != ofproto.OFPP_FLOOD:
            out_port = actions[0].port
            self.logger.info("Installing flow on switch %s: in_port=%s, dst=%s -> out_port=%s",
                             dpid, in_port, dst, out_port)
            match = parser.OFPMatch(in_port=in_port, eth_dst=dst)
            self.add_flow(datapath, 1, match, actions, idle=10, hard=30)

        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data
        out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                  in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)

    def _get_neighbor_by_port(self, dpid, port):
        for neighbor in self.switch_net.neighbors(dpid):
            if self.switch_net[dpid][neighbor]['port'] == port:
                return neighbor
        return None

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change_handler(self, ev):
        datapath = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            if datapath.id not in self.datapaths:
                self.datapaths[datapath.id] = datapath
                self.logger.info("Datapath %d registered", datapath.id)
        elif ev.state == DEAD_DISPATCHER:
            if datapath.id in self.datapaths:
                del self.datapaths[datapath.id]
                self.logger.warning("Datapath %d unregistered", datapath.id)
