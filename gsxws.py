#!/usr/bin/env python

#coding=utf-8

import re
import os
import json
import base64
import suds
from suds.client import Client
from datetime import datetime, date, time
import xml.etree.ElementTree as ET

import logging
logging.basicConfig(level=logging.INFO)

logging.getLogger('suds.client').setLevel(logging.DEBUG)

# Must use a few module-level global variables
CLIENT = None
SESSION = dict()
LOCALE = 'en_XXX'

def validate(value, what=None):
    """
    Tries to guess the meaning of value or validate that
    value looks like what it's supposed to be.
    """
    result = None

    if not isinstance(value, basestring):
        raise ValueError('%s is not valid input')

    rex = {
        'partNumber'        : r'^([A-Z]{1,2})?\d{3}\-?(\d{4}|[A-Z]{2})(/[A-Z])?$',
        'serialNumber'      : r'^[A-Z0-9]{11,12}$',
        'eeeCode'           : r'^[A-Z0-9]{3,4}$',
        'returnOrder'       : r'^7\d{9}$',
        'repairNumber'      : r'^\d{12}$',
        'dispatchId'        : r'^G\d{9}$',
        'alternateDeviceId' : r'^\d{15}$',
        'diagnosticEventNumber': r'^\d{23}$',
        'productName'       : r'^i?Mac',
    }
    
    for k, v in rex.items():
        if re.match(v, value):
            result = k

    return (result == what) if what else result

def get_format(locale=LOCALE):
    df = open(os.path.join(os.path.dirname(__file__), 'langs.json'), 'r')
    data = json.load(df)

    return data[locale]

class GsxObject(object):
    """
    The thing that gets sent to and from GSX
    """
    dt          = 'ns3:authenticateRequestType'   # The GSX datatype matching this object
    request_dt  = 'ns3:authenticateRequestType'   # The GSX datatype matching this request
    method      = 'Authenticate'                  # The SOAP method to call on the GSX server

    def __init__(self, *args, **kwargs):
        self.data = kwargs
        self.dt = CLIENT.factory.create(self.dt)
        self.request_dt = CLIENT.factory.create(self.request_dt)

    def set_method(self, new_method):
        self.method = new_method

    def set_type(self, new_dt):
        """
        Sets the object's primary data type to new_dt
        """
        self.dt = self._make_type(new_dt)

        try:
            for k, v in self.data.items():
                setattr(self.dt, k, v)
        except Exception, e:
            pass

    def set_request(self, new_dt=None, field=None):
        """
        Sets the field of this object's request datatype to the new value
        """
        if new_dt:
            self.request_dt = self._make_type(new_dt)

        setattr(self.request_dt, field, self.dt)

    def submit(self, method, data, attr=None):
        """
        Submits the SOAP envelope
        """
        f = getattr(CLIENT.service, method)
        
        try:
            result = f(data)
            return getattr(result, attr) if attr else result
        except suds.WebFault, e:
            raise GsxError(code=e.fault.faultcode, message=e.fault.faultstring)

    def _make_type(self, new_dt):
        """
        Creates the top-level datatype for the API call
        """
        dt = CLIENT.factory.create(new_dt)

        if SESSION:
            dt.userSession = SESSION

        return dt

    def _process(self, data):
        """
        Tries to coerce some types to their Python counterparts
        """
        for k, v in data:
            # decode binary data
            if k in ['packingList', 'proformaFileData', 'returnLabelFileData']:
                v = base64.b64decode(v)
            
            if isinstance(v, basestring):
                # convert dates to native Python
                if re.search('^\d{2}/\d{2}/\d{2}$', v):
                    m, d, y = v.split('/')
                    v = date(2000+int(y), int(m), int(d)).isoformat()

                # strip currency prefix and munge into float
                if re.search('Price$', k):
                    v = float(re.sub('[A-Z ,]', '', v))

            setattr(data, k, v)

        return data

    def get_response(self, xml_el):

        if isinstance(xml_el, list):
            out = []
            for i in xml_el:
                out.append(self.get_response(i))

            return out

        if isinstance(xml_el, dict):
            out = []
            for i in xml_el.items():
                out.append(self.get_response(i))

            return out

        class ReturnData(dict):
            pass

        rd = ReturnData()
        
        for r in xml_el.iter():
            k, v = r.tag, r.text
            if k in ['packingList', 'proformaFileData', 'returnLabelFileData']:
                v = base64.b64decode(v)

            setattr(rd, k, v)

        return rd


class CompTIA:
    """
    Stores and accesses CompTIA codes.
    """
    def __init__(self):
        df = open(os.path.join(os.path.dirname(__file__), 'comptia.json'))
        self.data = json.load(df)

    def fetch(self):
        """
        Here we must resort to raw XML parsing since SUDS throws this:
        suds.TypeNotFound: Type not found: 'comptiaDescription'
        when calling CompTIACodes()...
        """
        CLIENT.set_options(retxml=True)
        dt = CLIENT.factory.create('ns3:comptiaCodeLookupRequestType')
        dt.userSession = SESSION
        xml = CLIENT.service.CompTIACodes(dt)

        root = ET.fromstring(xml).findall('.//%s' % 'comptiaInfo')[0]

        # Process CompTIA Groups
        class ComptiaGroup:
            pass

        class ComptiaModifier:
            pass

        self.groups = list()
        self.modifiers = list()

        for el in root.findall('.//comptiaGroup'):
            dt = ComptiaGroup()
            self.groups.append(self.__process(el, dt))

        for el in root.findall('.//comptiaModifier'):
            mod = ComptiaModifier()
            self.modifiers.append(self.__process(el, mod))

    def __process(self, element, obj):
        for i in element.iter():
            setattr(obj, i.tag, i.text)

        return obj

    def symptoms(self, component=None):
        symptoms = self.data['symptoms']
        
        if component:
            r = list()
            for k, v in symptoms[component].items():
                r.append((k, v))
            return r

        return symptoms

    def modifiers(self):
        modifiers = list()

        for k, v in self.data['modifiers'].items():
            modifiers.append((k, v))

        return modifiers

class GsxResponse(dict):
    """
    This contains the data returned by a raw GSX query
    """
    def __getattr__(self, item):
        return self.__getitem__(item)

    def __setattr__(self, item, value):
        self.__setitem__(item, value)

    @classmethod
    def Process(cls, node):
        nodedict = cls()
        
        for child in node:
            k, v = child.tag, child.text
            newitem = cls.Process(child)

            if nodedict.has_key(k):
                # found duplicate tag
                if isinstance(nodedict[k], list):
                    # append to existing list
                    nodedict[k].append(newitem)
                else:
                    # convert to list
                    nodedict[k] = [nodedict[k], newitem]
            else:
                # unique tag -> set the dictionary
                nodedict[k] = newitem

            if k in ('packingList', 'proformaFileData', 'returnLabelFileData'):
                nodedict[k] = base64.b64decode(v)

            if isinstance(v, basestring):
                # convert dates to native Python type
                if re.search('^\d{2}/\d{2}/\d{2}$', v):
                    m, d, y = v.split('/')
                    v = date(2000+int(y), int(m), int(d)).isoformat()

                # strip currency prefix and munge into float
                if re.search('Price$', k):
                    v = float(re.sub('[A-Z ,]', '', v))

                # Convert timestamps to native Python type
                # 18-Jan-13 14:38:04
                if re.search('TimeStamp$', k):
                    v = datetime.strptime(v, '%d-%b-%y %H:%M:%S')

                # convert Y and N to corresponding boolean
                if re.search('^[YN]$', k):
                    v = (v == 'Y')

                nodedict[k] = v

        return nodedict

class GsxError(Exception):
    def __init__(self, message, code=None):
        self.code = code
        self.message = message
        self.data = (self.code, self.message)

    def __getitem__(self, idx):
        return self.data[idx]

    def __repr__(self):
        print self.data

    def __str__(self):
        return self.data[1]

class Lookup(GsxObject):
    def parts(self):
        """
        The Parts Lookup API allows users to access part and part pricing data prior to 
        creating a repair or order. Parts lookup is also a good way to search for 
        part numbers by various attributes of a part
        (config code, EEE code, serial number, etc.). 
        """
        dt = self._make_type("ns0:partsLookupRequestType")
        dt.lookupRequestData = self.data
        return self.submit("PartsLookup", dt, "parts")

    def repairs(self):
        """
        The Repair Lookup API mimics the front-end repair search functionality.
        It fetches up to 2500 repairs in a given criteria.
        Subsequently, the extended Repair Status API can be used to retrieve more details of the repair.
        """
        dt = CLIENT.factory.create('ns6:repairLookupInfoType')
        request = CLIENT.factory.create('ns1:repairLookupRequestType')
        request.userSession = SESSION
        request.lookupRequestData = self.data
        return self.submit("RepairLookup", request, "lookupResponseData")

class Diagnostics(GsxObject):
    def fetch(self):
        """
        The Fetch Repair Diagnostics API allows the service providers/depot/carriers 
        to fetch MRI/CPU diagnostic details from the Apple Diagnostic Repository OR 
        diagnostic test details of iOS Devices.
        The ticket is generated within GSX system.
        """
        # Using raw XML to avoid TypeNotFound: Type not found: 'toolID' or operationID
        CLIENT.set_options(retxml=True)
        if "alternateDeviceId" in self.data:
            dt = self._make_type("ns3:fetchIOSDiagnosticRequestType")
            dt.lookupRequestData = self.data
            result = CLIENT.service.FetchIOSDiagnostic(dt)
            root = ET.fromstring(result).findall('*//%s' % 'FetchIOSDiagnosticResponse')[0]
        else:
            dt = self._make_type('ns3:fetchRepairDiagnosticRequestType')
            dt.lookupRequestData = self.data
            result = CLIENT.service.FetchRepairDiagnostic(dt)
            root = ET.fromstring(result).findall('*//%s' % 'FetchRepairDiagnosticResponse')[0]
        
        return GsxResponse.Process(root)

class Order(GsxObject):
    def __init__(self, type='stocking', *args, **kwargs):
        super(Order, self).__init__(*args, **kwargs)
        self.data['orderLines'] = list()

    def add_part(self, part_number, quantity):
        self.data['orderLines'].append({
            'partNumber': part_number, 'quantity': quantity
            })

    def submit(self):
        dt = CLIENT.factory.create('ns1:createStockingOrderRequestType')
        dt.userSession = SESSION
        dt.orderData = self.data
        result = CLIENT.service.CreateStockingOrder(dt)
        return result.orderConfirmation

class Returns(GsxObject):
    def __init__(self, order_number=None, *args, **kwargs):
        super(Returns, self).__init__(*args, **kwargs)
        self.dt.returnOrderNumber = order_number

    def get_report(self):
        """
        The Return Report API returns a list of all parts that are returned 
        or pending for return, based on the search criteria. 
        """
        pass

    def get_label(self, part_number):
        """
        The Return Label API retrieves the Return Label for a given Return Order Number.
        This is another example where SUDS doesn't play nice with GSX WS (Type not found: 'comptiaCode')
        so we're parsing the raw SOAP response and creating a "fake" return object from that.
        """
        if not validate(part_number, 'partNumber'):
            raise ValueError('%s is not a valid part number' % part_number)

        class ReturnData(dict):
            pass

        rd = ReturnData()

        CLIENT.set_options(retxml=True)

        dt = CLIENT.factory.create('ns1:returnLabelRequestType')
        dt.returnOrderNumber = self.dt.returnOrderNumber
        dt.partNumber = part_number
        dt.userSession = SESSION

        result = CLIENT.service.ReturnLabel(dt)
        el = ET.fromstring(result).findall('*//%s' % 'returnLabelData')[0]

        for r in el.iter():
            k, v = r.tag, r.text
            if k in ['packingList', 'proformaFileData', 'returnLabelFileData']:
                v = base64.b64decode(v)

            setattr(rd, k, v)

        return rd

    def get_proforma(self):
        """
        The View Bulk Return Proforma API allows you to view 
        the proforma label for a given Bulk Return Id.
        You can create a parts bulk return by using the Register Parts for Bulk Return API. 
        """

    def register_parts(self):
        """
        The Register Parts for Bulk Return API creates a bulk return for 
        the registered parts.
        The API returns the Bulk Return Id with the packing list.
        """

    def get_pending(self):
        """
        The Parts Pending Return API returns a list of all parts that 
        are pending for return, based on the search criteria. 
        """
        dt = CLIENT.factory.create('ns1:partsPendingReturnRequestType')
        dt.repairData = self.data
        dt.userSession = SESSION
        result = CLIENT.service.PartsPendingReturn(dt)
        return result.partsPendingResponse

class Part(GsxObject):
    def lookup(self):
        lookup = Lookup(**self.data)
        return lookup.parts()

class Escalation(GsxObject):
    def create(self):
        """
        The Create General Escalation API allows users to create 
        a general escalation in GSX. The API was earlier known as GSX Help.
        """
        dt = self._make_type("ns1:createGenEscRequestType")
        dt.escalationRequest = self.data
        return self.submit("CreateGeneralEscalation", dt, "escalationConfirmation")

    def update(self):
        """
        The Update General Escalation API allows Depot users to 
        update a general escalation in GSX.
        """
        dt = self._make_type("ns1:updateGeneralEscRequestType")
        dt.escalationRequest = self.data
        return self.submit("UpdateGeneralEscalation", dt, "escalationConfirmation")

class Repair(GsxObject):
    
    dt = 'ns6:repairLookupInfoType'
    request_dt = 'ns1:repairLookupRequestType'

    def __init__(self, *args, **kwargs):
        super(Repair, self).__init__(*args, **kwargs)
        formats = get_format()

        # native types are not welcome here :)
        for k, v in kwargs.items():
            if isinstance(v, date):
                kwargs[k] = v.strftime(formats['df'])
            if isinstance(v, time):
                kwargs[k] = v.strftime(formats['tf'])
            if isinstance(v, bool):
                kwargs[k] = 'Y' if v else 'N'
        
        self.data = kwargs

    def create_carryin(self):
        """
        GSX validates the information and if all of the validations go through,
        it obtains a quote for the repair and creates the carry-in repair.
        """
        dt = self._make_type('ns2:carryInRequestType')
        dt.repairData = self.data
        return self.submit('CreateCarryInRepair', dt, 'repairConfirmation')
        
    def create_cnd(self):
        """
        The Create CND Repair API allows Service Providers to create a repair 
        whenever the reported issue cannot be duplicated, and the repair 
        requires no parts replacement.
        N01 Unable to Replicate
        N02 Software Update/Issue
        N03 Cable/Component Reseat
        N05 SMC Reset
        N06 PRAM Reset
        N07 Third Party Part
        N99 Other
        """
        pass

    def update_carryin(self):
        """
        The Update Carry-In Repair API allows the service providers 
        to update the existing  open carry-in repairs. 
        This API assists in addition/deletion of parts and addition of notes 
        to a repair. On successful update, the repair confirmation number and 
        quote for any newly added parts  would be returned.
        In case of any validation error or unsuccessful update, a fault code is issued.

        Carry-In Repair Update Status Codes:
        AWTP    Awaiting Parts
        AWTR    Parts Allocated
        BEGR    In Repair
        RFPU    Ready for Pickup
        """
        pass

    def update_kgb_sn(self, sn):
        """
        Description:
        The KGB Serial Number Update API is always to be used on 
        whole unit repairs that are in a released state. 
        This API allows users to provide the KGB serial number for the 
        whole unit exchange repairs. It also checks for the privilege 
        to create/ update whole unit exchange repairs 
        before updating the whole unit exchange repair. 
        Context:
        The API is to be used on whole unit repairs that are in a released state. 
        This API can be invoked only after carry-in repair creation API.
        """

        # Using raw XML to avoid:
        # Exception: <UpdateKGBSerialNumberResponse/> not mapped to message part
        CLIENT.set_options(retxml=True)
        dt = self._make_type('ns1:updateKGBSerialNumberRequestType')
        dt.repairConfirmationNumber = self.data['dispatchId']
        dt.serialNumber = sn

        result = CLIENT.service.KGBSerialNumberUpdate(dt)
        root = ET.fromstring(result).findall('*//%s' % 'UpdateKGBSerialNumberResponse')
        return GsxResponse.Process(root[0])

    def lookup(self):
        """
        The Repair Lookup API mimics the front-end repair search functionality.
        It fetches up to 2500 repairs in a given criteria.
        Subsequently, the extended Repair Status API can be used 
        to retrieve more details of the repair. 
        """
        return Lookup(**self.data).repairs()

    def mark_complete(self, numbers=None):
        """
        The Mark Repair Complete API allows a single or an array of 
        repair confirmation numbers to be submitted to GSX to be marked as complete.
        """
        dt = self._make_type('ns1:markRepairCompleteRequestType')
        dt.repairConfirmationNumbers = [self.data['dispatchId']]
        result = CLIENT.service.MarkRepairComplete(dt)

        return result.repairConfirmationNumbers

    def delete(self):
        """
        The Delete Repair API allows the service providers to delete 
        the existing GSX Initiated Carry-In, Return Before Replace & Onsite repairs 
        which are in Declined-Rejected By TSPS Approver state, 
        that do not have an active repair id.
        """
        pass

    def get_status(self, numbers=None):
        """
        The Repair Status API retrieves the status
        for the submitted repair confirmation number(s).
        """
        dt = self._make_type('ns1:repairStatusRequestType')
        dt.repairConfirmationNumbers = [self.data['dispatchId']]
        result = CLIENT.service.RepairStatus(dt)

        if len(result.repairStatus) == 1:
            return result.repairStatus[0]
        else:
            return result.repairStatus

    def get_details(self):
        """
        The Repair Details API includes the shipment information 
        similar to the Repair Lookup API. 
        """
        dt = self._make_type('ns0:repairDetailsRequestType')
        dt.dispatchId = self.data['dispatchId']
        results = CLIENT.service.RepairDetails(dt)
        details = results.lookupResponseData[0]

        # fix tracking URL if available
        for i, p in enumerate(details.partsInfo):
            try:
                url = re.sub('<<TRKNO>>', p.deliveryTrackingNumber, p.carrierURL)
                details.partsInfo[i].carrierURL = url
            except AttributeError, e:
                pass

        return details

class Communication(GsxObject):
    def get_content():
        """
        The Fetch Communication Content API allows the service providers/depot/carriers
        to fetch the communication content by article ID from the service news channel. 
        """

    def get_articles():
        """
        The Fetch Communication Articles API allows the service partners
        to fetch all the active communication message IDs. 
        """

class Product(GsxObject):
    
    dt = 'ns7:unitDetailType'

    def __init__(self, serialNumber):
        super(Product, self).__init__()
        self.serialNumber = serialNumber
        self.dt.serialNumber = serialNumber
        self.lookup = Lookup(serialNumber=self.serialNumber)

    def get_model(self):
        """
        This API allows Service Providers/Carriers to fetch
        Product Model information for the given serial number.
        """
        #self.set_request('ns3:fetchProductModelRequestType', 'productModelRequest')
        dt = self._make_type("ns3:fetchProductModelRequestType")
        dt.productModelRequest = self.dt
        result = self.submit('FetchProductModel', dt, "productModelResponse")
        return result

    def get_warranty(self, date_received=None, parts=[]):
        """
        The Warranty Status API retrieves the same warranty details
        displayed on the GSX Coverage screen.
        If part information is provided, the part warranty information is returned.
        If you do not provide the optional part information in the
        warranty status request, the unit level warranty information is returned.
        """
        dt = self._make_type("ns3:warrantyStatusRequestType")
        dt.unitDetail = self.dt
        result = self.submit("WarrantyStatus", dt, "warrantyDetailInfo")
        return self._process(result)

    def get_activation(self):
        """
        The Fetch iOS Activation Details API is used to 
        fetch activation details of iOS Devices. 
        """
        dt = self._make_type('ns3:fetchIOSActivationDetailsRequestType')
        dt.serialNumber = self.serialNumber
        return self.submit('FetchIOSActivationDetails', dt, 'activationDetailsInfo')

    def get_parts(self):
        return self.lookup.parts()

    def get_repairs(self):
        return self.lookup.repairs()

    def get_diagnostics(self):
        diags = Diagnostics(serialNumber=self.serialNumber)
        return diags.fetch()

def init(env='ut', region='emea'):
    global CLIENT

    envs = ('pr', 'it', 'ut',)
    hosts = {'pr': 'ws2', 'it': 'wsit', 'ut': 'wsut'}

    if env not in envs:
        raise ValueError('Environment should be one of: %s' % ','.join(envs))

    url = 'https://gsx{env}.apple.com/wsdl/{region}Asp/gsx-{region}Asp.wsdl'
    url = url.format(env=hosts[env], region=region)

    CLIENT = Client(url)

def connect(
        user_id,
        password,
        sold_to, 
        language='en',
        timezone='CEST', 
        environment='ut',
        region='emea',
        locale=LOCALE):

    global SESSION
    global LOCALE

    SESSION = {}
    LOCALE = LOCALE

    init(environment, region)

    account = CLIENT.factory.create('ns3:authenticateRequestType')

    account.userId = user_id
    account.password = password
    account.languageCode = language
    account.userTimeZone = timezone
    account.serviceAccountNo = sold_to

    result = CLIENT.service.Authenticate(account)
    SESSION['userSessionId'] = result.userSessionId

    return SESSION

def logout():
    CLIENT.service.Logout()

if __name__ == '__main__':
    import sys
    import json
    import argparse
    
    parser = argparse.ArgumentParser(description='Communicate with GSX Web Services')

    parser.add_argument('user_id')
    parser.add_argument('password')
    parser.add_argument('sold_to')
    parser.add_argument('--language', default='en')
    parser.add_argument('--timezone', default='CEST')
    parser.add_argument('--environment', default='pr')
    parser.add_argument('--region', default='emea')
    args = parser.parse_args()

    #connect(**vars(args))

    #f = 'tests/create_carryin_repair.json'
    #f = 'tests/warranty_status.json'
    #f = 'tests/fetch_ios_activation.json'
    #f = 'tests/update_escalation.json'
    #fp = open(f, 'r')
    #data = json.load(fp)

    #try:
    #    print Escalation(**data).update()
    #except GsxError, e:
    #    print e.code, e.message
    
    #data['requestReviewByApple'] = False
    #rep = Repair(dispatchId='')
    #print rep.update_kgb_sn('')