import re
import os
import requests

from decimal import Decimal, InvalidOperation
from django import db
from django.core.files.storage import FileSystemStorage
from osgeo_importer.handlers import ImportHandlerMixin, GetModifiedFieldsMixin, ensure_can_run
from geoserver.catalog import FailedRequestError, ConflictingDataError
from geonode.geoserver.helpers import gs_catalog
from geoserver.support import DimensionInfo
from osgeo_importer.utils import CheckFile, increment_filename
import logging
log = logging.getLogger(__name__)

MEDIA_ROOT = FileSystemStorage().location


def configure_time(resource, name='time', enabled=True, presentation='LIST', resolution=None, units=None,
                   unitSymbol=None, **kwargs):
    """
    Configures time on a geoserver resource.
    """
    time_info = DimensionInfo(name, enabled, presentation, resolution, units, unitSymbol, **kwargs)
    resource.metadata = {'time': time_info}
    return resource.catalog.save(resource)


class GeoserverHandlerMixin(ImportHandlerMixin):
    """
    A Mixin for Geoserver handlers.
    """
    catalog = gs_catalog


class GeoServerTimeHandler(GetModifiedFieldsMixin, GeoserverHandlerMixin):
    """
    Enables time in Geoserver for a layer.
    """

    def can_run(self, layer, layer_config, *args, **kwargs):
        """
        Returns true if the configuration has enough information to run the handler.
        """

        if not layer_config.get('configureTime', None):
            return False

        if not any([layer_config.get('start_date', None), layer_config.get('end_date', None)]):
            return False

        return True

    @ensure_can_run
    def handle(self, layer, layer_config, *args, **kwargs):
        """
        Configures time on the object.

        Handler specific params:
        "configureTime": Must be true for this handler to run.
        "start_date": Passed as the start time to Geoserver.
        "end_date" (optional): Passed as the end attribute to Geoserver.
        """

        lyr = self.catalog.get_layer(layer)
        self.update_date_attributes(layer_config)
        configure_time(lyr.resource, attribute=layer_config.get('start_date'),
                       end_attribute=layer_config.get('start_date'))


class GeoserverPublishHandler(GeoserverHandlerMixin):
    workspace = 'geonode'
    srs = 'EPSG:4326'

    def can_run(self, layer, layer_config, *args, **kwargs):
        """
        Returns true if the configuration has enough information to run the handler.
        """
        if re.search(r'\.tif$', layer):
            return False

        return True

    def get_default_store(self):
        connection = db.connections['datastore']
        settings = connection.settings_dict

        return {
              'database': settings['NAME'],
              'passwd': settings['PASSWORD'],
              'namespace': 'http://www.geonode.org/',
              'type': 'PostGIS',
              'dbtype': 'postgis',
              'host': settings['HOST'],
              'user': settings['USER'],
              'port': settings['PORT'],
              'enabled': 'True',
              'name': settings['NAME']}

    def get_or_create_datastore(self, layer_config):
        connection_string = layer_config.get('geoserver_store', self.get_default_store())

        try:
            return self.catalog.get_store(connection_string['name'])
        except FailedRequestError:
            store = self.catalog.create_datastore(connection_string['name'], workspace=self.workspace)
            store.connection_parameters.update(connection_string)
            self.catalog.save(store)

        return self.catalog.get_store(connection_string['name'])

    def geogig_handler(self, store, layer, layer_config):

        repo = store.connection_parameters['geogig_repository']
        auth = (self.catalog.username, self.catalog.password)
        repo_url = self.catalog.service_url.replace('/rest', '/geogig/{0}/'.format(repo))
        transaction = requests.get(repo_url + 'beginTransaction.json', auth=auth)
        transaction_id = transaction.json()['response']['Transaction']['ID']
        params = self.get_default_store()
        params['password'] = params['passwd']
        params['table'] = layer
        params['transactionId'] = transaction_id

        import_command = requests.get(repo_url + 'postgis/import.json', params=params, auth=auth)
        task = import_command.json()['task']

        status = 'NOT RUN'
        while status != 'FINISHED':
            check_task = requests.get(task['href'], auth=auth)
            status = check_task.json()['task']['status']

        if check_task.json()['task']['status'] == 'FINISHED':
            requests.get(repo_url + 'add.json', params={'transactionId': transaction_id}, auth=auth)
            requests.get(repo_url + 'commit.json', params={'transactionId': transaction_id}, auth=auth)
            requests.get(repo_url + 'endTransaction.json', params={'transactionId': transaction_id}, auth=auth)

    @ensure_can_run
    def handle(self, layer, layer_config, *args, **kwargs):
        """
        Publishes a layer to GeoServer.

        Handler specific params:
        "geoserver_store": Connection parameters used to get/create the geoserver store.
        "srs": The native srs authority and code (ie EPSG:4326) for this data source.
        """
        store = self.get_or_create_datastore(layer_config)

        if getattr(store, 'type', '').lower() == 'geogig':
            self.geogig_handler(store, layer, layer_config)
        ft = self.catalog.publish_featuretype(layer, self.get_or_create_datastore(layer_config),
                                              layer_config.get('srs', self.srs))
        ftjs = {'title': ft.title, 'projection': ft.projection, 'attributes': ft.attributes}
        return ftjs


class GeoserverPublishCoverageHandler(GeoserverHandlerMixin):
    workspace = 'geonode'

    def can_run(self, layer, layer_config, *args, **kwargs):
        """
        Returns true if the configuration has enough information to run the handler.
        """
        if re.search(r'\.tif$', layer):
            return True

        return False

    @ensure_can_run
    def handle(self, layer, layer_config, *args, **kwargs):
        """
        Publishes a Coverage layer to GeoServer.
        """
        name = os.path.splitext(os.path.basename(layer))[0]
        workspace = self.catalog.get_workspace(self.workspace)

        return self.catalog.create_coveragestore(name, layer, workspace, False)


class GeoWebCacheHandler(GeoserverHandlerMixin):
    """
    Configures GeoWebCache for a layer in Geoserver.
    """

    @staticmethod
    def config(**kwargs):
        return """<?xml version="1.0" encoding="UTF-8"?>
            <GeoServerLayer>
              <name>{name}</name>
              <enabled>true</enabled>
              <mimeFormats>
                <string>image/png</string>
                <string>image/jpeg</string>
                <string>image/png8</string>
              </mimeFormats>
              <gridSubsets>
                <gridSubset>
                  <gridSetName>EPSG:900913</gridSetName>
                </gridSubset>
                <gridSubset>
                  <gridSetName>EPSG:4326</gridSetName>
                </gridSubset>
                <gridSubset>
                  <gridSetName>EPSG:3857</gridSetName>
                </gridSubset>
              </gridSubsets>
              <metaWidthHeight>
                <int>4</int>
                <int>4</int>
              </metaWidthHeight>
              <expireCache>0</expireCache>
              <expireClients>0</expireClients>
              <parameterFilters>
                {regex_parameter_filter}
                <styleParameterFilter>
                  <key>STYLES</key>
                  <defaultValue/>
                </styleParameterFilter>
              </parameterFilters>
              <gutter>0</gutter>
            </GeoServerLayer>""".format(**kwargs)

    def can_run(self, layer, layer_config, *args, **kwargs):
        """
        Only run this handler if the layer is found in Geoserver.
        """
        self.layer = self.catalog.get_layer(layer)

        if self.layer:
            return True

        return

    @staticmethod
    def time_enabled(layer):
        """
        Returns True is time is enabled for a Geoserver layer.
        """
        return 'time' in (getattr(layer.resource, 'metadata', []) or [])

    def gwc_url(self, layer):
        """
        Returns the GWC URL given a Geoserver layer.
        """

        return self.catalog.service_url.replace('rest', 'gwc/rest/layers/{workspace}:{layer_name}.xml'.format(
            workspace=layer.resource.workspace.name, layer_name=layer.name))

    @ensure_can_run
    def handle(self, layer, layer_config, *args, **kwargs):
        """
        Adds a layer to GWC.
        """
        regex_filter = ""
        time_enabled = self.time_enabled(self.layer)

        if time_enabled:
            regex_filter = """
                <regexParameterFilter>
                  <key>TIME</key>
                  <defaultValue/>
                  <regex>.*</regex>
                </regexParameterFilter>
                """

        return self.catalog.http.request(self.gwc_url(self.layer), method="POST",
                                         body=self.config(regex_parameter_filter=regex_filter, name=self.layer.name))


class GeoServerBoundsHandler(GeoserverHandlerMixin):
    """
    Sets the lat/long bounding box of a layer to the max extent of WGS84 if the values of the current lat/long
    bounding box fail the Decimal quantize method (which Django uses internally when validating decimals).

    This can occur when the native bounding box contain Infinity values.
    """

    def can_run(self, layer, layer_config, *args, **kwargs):
        """
        Only run this handler if the layer is found in Geoserver.
        """
        self.catalog._cache.clear()
        self.layer = self.catalog.get_layer(layer)

        if self.layer:
            return True

        return

    @ensure_can_run
    def handle(self, layer, layer_config, *args, **kwargs):
        resource = self.layer.resource
        try:
            for dec in map(Decimal, resource.latlon_bbox[:4]):
                dec.quantize(1)

        except InvalidOperation:
            resource.latlon_bbox = ['-180', '180', '-90', '90', 'EPSG:4326']
            self.catalog.save(resource)


class GeoServerStyleHandler(GeoserverHandlerMixin):
    """
    Adds styles to GeoServer Layer
    """
    catalog = gs_catalog
    catalog._cache.clear()
    workspace = 'geonode'

    def can_run(self, layer, layer_config, *args, **kwargs):
        """
        Returns true if the configuration has enough information to run the handler.
        """
        if not any([layer_config.get('default_style', None), layer_config.get('styles', None)]):
            log.debug('Could not find any styles in config %s', layer_config)
            return False

        return True

    @ensure_can_run
    def handle(self, layer, layer_config, *args, **kwargs):
        """
        Handler specific params:
        "default_sld": SLD to load as default_sld
        "slds": SLDS to add to layer
        """
        log.debug(layer, layer_config)
        lyr = self.catalog.get_layer(layer)
        path = "%s/uploads/%s" % (MEDIA_ROOT, layer_config.get('upload_id'))
        log.debug(path)
        default_sld = layer_config.get('default_style', None)
        slds = layer_config.get('styles', None)
        all_slds = []
        if default_sld is not None:
            slds.append(default_sld)

        all_slds = list(set(slds))
        all_slds = [CheckFile(x) for x in all_slds if x is not None]

        styles = []
        default_style = None
        for sld in all_slds:
            with open("%s/%s" % (path, sld.name)) as s:
                n = 0
                sldname = sld.root
                while True:
                    n += 1
                    try:
                        self.catalog.create_style(sldname, s.read(), overwrite=False, workspace=self.workspace)
                    except ConflictingDataError:
                        sldname = increment_filename(sldname)
                    if n >= 100:
                        break

                style = self.catalog.get_style(sld.root, workspace=self.workspace)
                if sld.name == default_sld:
                    default_style = style
                styles.append(style)

        lyr.styles = list(set(lyr.styles + styles))
        if default_style is not None:
            lyr.default_style = default_style
        self.catalog.save(lyr)
        return {'default_style': default_style.filename}


class GenericSLDHandler(GeoserverHandlerMixin):
    """
    Handles cases in Geoserver 2.8x+ where the generic sld is used.  The generic style causes service exceptions.
    """

    def can_run(self, layer, layer_config, *args, **kwargs):
        """
        Only run this handler if the layer is found in Geoserver and the layer's style is the generic style.
        """
        self.catalog._cache.clear()
        self.layer = self.catalog.get_layer(layer)

        return self.layer and self.layer.default_style and self.layer.default_style.name == 'generic'

    @ensure_can_run
    def handle(self, layer, layer_config, *args, **kwargs):
        """
        Replace the generic layer with the 'point' layer.
        """
        self.layer.default_style = 'point'
        self.catalog.save(self.layer)
