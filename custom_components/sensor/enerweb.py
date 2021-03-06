# -*- coding: utf-8 -*-
"""
Custom sensor component to access another custom data logger (`enerweb`)
It updates sensor last values accessing them directly via remote MYSQL DB.

```
    SQL_ALCHEMY URI: mysql+cymysql://{}:{}@{}/data_enerweb

    SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = "{}"
    SELECT * FROM {} ORDER BY ID DESC LIMIT {}
```
"""
import asyncio
from collections import deque
from functools import partial
from datetime import timedelta
import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import (OperationalError, InternalError,
                            TimeoutError, SQLAlchemyError)
import voluptuous as vol

from homeassistant.components.sensor import PLATFORM_SCHEMA
from homeassistant.const import (CONF_HOST, TEMP_CELSIUS, CONF_SENSORS,
                                 CONF_TIMEOUT, CONF_NAME,
                                 STATE_UNKNOWN, STATE_ON, STATE_OFF)
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import (async_track_time_interval,
                                         async_track_point_in_utc_time)
from homeassistant.util import slugify
from homeassistant.util.dt import now

_LOGGER = logging.getLogger(__name__)
REQUIREMENTS = ['sqlalchemy']

CONF_ROUND = 'round'
CONF_MYSQL_USER = 'mysql_user'
CONF_MYSQL_PASS = 'mysql_password'
DEFAULT_NAME = 'enerweb'
DEFAULT_TIMEOUT = 10

URL_MASK_ENERWEB_GET_DATA = 'http://{}/enerweb/get_sensors_info?samples=1'
URL_MASK_ENERWEB_GET_DATA_MYSQL = 'mysql://{}:{}@{}/data_enerweb'
SQLMASK_SELECT_TABLE_COLUMN_NAMES = 'SELECT COLUMN_NAME FROM ' \
                                    'INFORMATION_SCHEMA.COLUMNS ' \
                                    'WHERE TABLE_NAME = \"{}\"'
SQLMASK_SELECT_TABLE_LAST_VALUES = 'SELECT * FROM {} ORDER BY ID DESC LIMIT {}'

SCAN_INTERVAL = timedelta(seconds=40)

KEY_TIMESTAMP = 'ts'
SENSOR_TYPES_UNITS = {
    'ds18b20': ['temperature', TEMP_CELSIUS],
    'dht22': {'temp': ['temperature', TEMP_CELSIUS], 'hum': ['humidity', '%']},
    'dht11': {'temp': ['temperature', TEMP_CELSIUS], 'hum': ['humidity', '%']},
    'rpi2': {'rpit': ['temperature', TEMP_CELSIUS],
             'temp': ['temperature', TEMP_CELSIUS],
             'tempp': ['temperature', TEMP_CELSIUS],
             'pres': ['pressure', 'mb'], 'hr': ['humidity', '%']}}
JSON_MYSQL_TRANSLATION = {
    'ds18b20': ['measureds18b20', {'temp': 'sensor_id'}],
    'dht22': ['measuredht22', {'temp': 'temperature', 'hum': 'humidity'}],
    'dht11': ['measuredht11', {'temp': 'temperature', 'hum': 'humidity'}],
    'rpi2': ['hostmeasure', {'rpit': 'rpi_temp_cpu', 'temp': 'sense_temp',
                             'tempp': 'sense_tempp', 'pres': 'sense_press',
                             'hr': 'sense_hr'}]}

SQL_COLUMNS_TABLES = {
    'dht22': ('id', 'ts', 'temperature', 'humidity', 'exec_id'),
    'ds18b20': ('id', 'ts', 'temperature', 'sensor_id'),
    'rpi2': ('id', 'ts', 'rpi_temp_cpu', 'sensehat', 'sense_temp',
             'sense_pres', 'sense_hr', 'sense_tempp', 'exec_id')
}

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_HOST): cv.string,
    vol.Required(CONF_SENSORS): vol.All(cv.ensure_list, [cv.ensure_list]),
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    vol.Optional(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): cv.positive_int,
    # vol.Optional(CONF_SCAN_INTERVAL): cv.positive_int,
    vol.Optional(CONF_MYSQL_USER, default=None): cv.string,
    vol.Optional(CONF_MYSQL_PASS, default=None): cv.string,
    vol.Optional(CONF_ROUND, default=None): cv.positive_int,
})

# Heating system states:
STATE_HEATING_UNKNOWN = -1
STATE_HEATING_OFF = 0
STATE_HEATING_ON = 1
STATE_HEATING_STOP = 2
STATE_HEATING_RESTART = 3
STATE_HEATING_COLD_START = 4
D_STATES_HEATING = {STATE_HEATING_UNKNOWN: STATE_UNKNOWN,
                    STATE_HEATING_OFF: STATE_OFF,
                    STATE_HEATING_COLD_START: 'Arranque',
                    STATE_HEATING_ON: STATE_ON,
                    STATE_HEATING_STOP: 'Parada',
                    STATE_HEATING_RESTART: 'Rearranque'}


# noinspection PyUnusedLocal
@asyncio.coroutine
def async_setup_platform(hass, config, async_add_devices, discovery_info=None):
    """Setup the Enerweb sensor platform."""
    enerweb_host = config[CONF_HOST]
    sensors_append = config[CONF_SENSORS]
    name = config[CONF_NAME]
    timeout = config[CONF_TIMEOUT]
    # scan_interval = config[CONF_SCAN_INTERVAL]
    round_result = config[CONF_ROUND]

    # MySQL remote access (vs requests to enerweb API)
    mysql_user = config[CONF_MYSQL_USER]
    mysql_pass = config[CONF_MYSQL_PASS]
    use_mysql = mysql_user is not None
    time_zone = hass.config.time_zone

    # Monitored enerweb sensors
    dev = []
    for sensors_type in sensors_append:
        for sensor_type, sensors_type_i in sensors_type[0].items():
            units_type = SENSOR_TYPES_UNITS[sensor_type]
            for sensor_mag, sensor_name in sensors_type_i.items():
                if type(units_type) is dict:
                    sensor_class, sensor_unit = units_type[sensor_mag]
                else:
                    sensor_class, sensor_unit = units_type
                _LOGGER.info('* Found sensor_type={}: sensor_mag={}, '
                             'sensor_name={}, unit={}'
                             .format(sensor_type, sensor_mag,
                                     sensor_name, sensor_unit))
                dev.append((sensor_type, sensor_mag, sensor_name, sensor_unit,
                            sensor_class, round_result))
    if dev:
        data_handler = yield from hass.async_add_job(
            EnerwebData, hass, enerweb_host, dev, time_zone,
            timeout, mysql_user, mysql_pass)
        sensors = [EnerwebSensor(data_handler, use_mysql, name, s_type,
                                 s_mag, s_name, s_unit, s_class, round_r)
                   for s_type, s_mag, s_name, s_unit, s_class, round_r in dev]

        # Special sensor with ambient temp + supply & return pipes temp
        # --> Heating state estimator
        s_imp = list(filter(
            lambda x: 'impul' in x.friendly_name.lower(), sensors))[0]
        s_ret = list(filter(
            lambda x: 'retorno' in x.friendly_name.lower(), sensors))[0]
        s_ref = list(filter(
            lambda x: 'dht22' in x.name.lower()
                      and x.unit_of_measurement == TEMP_CELSIUS, sensors))[0]
        # noinspection PyTypeChecker
        sensors.append(EnerwebHeaterState(data_handler, s_imp, s_ret, s_ref))
        async_add_devices(sensors)
    else:
        return False


class EnerwebSensor(Entity):
    """Representation of a Enerweb sensor."""

    def __init__(self, data_handler, use_mysql, name,
                 sensor_type, sensor_mag, sensor_friendly_name,
                 sensor_unit, sensor_class, round_result=None):
        """Initialize the sensor."""
        self._data = data_handler
        self._sensor_type = sensor_type
        self._sensor_mag = sensor_mag
        self._use_mysql = use_mysql
        self._mag_is_id = self._sensor_type.startswith('ds18b20')
        self.friendly_name = sensor_friendly_name
        self.sensor_class = sensor_class
        if self._mag_is_id:
            self._name = '{}_{}'.format(name, slugify(sensor_friendly_name))
        else:
            self._name = '{}_{}_{}'.format(name, sensor_type,
                                           slugify(sensor_friendly_name))
        self._unit_of_measurement = sensor_unit
        self._state = None
        self._last_update = None
        self._round = round_result
        self.async_update()

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def state(self):
        """Return the state of the sensor."""
        return self._state

    @property
    def unit_of_measurement(self):
        """Return the unit the value is expressed in."""
        return self._unit_of_measurement

    @asyncio.coroutine
    def async_update(self):
        """Get the latest data and updates the states."""
        # self._data.async_update()
        if self._data.last_sensor_data is not None:
            try:
                key_stype = self._sensor_type
                if not self._use_mysql:
                    key_stype += '_data'
                sensor_data = self._data.last_sensor_data[key_stype]
                if self._use_mysql and sensor_data:
                    self._last_update = sensor_data[KEY_TIMESTAMP]
                    self._state = sensor_data[self._sensor_mag]
                elif sensor_data:
                    if self._mag_is_id:
                        data_entity = list(filter(
                            lambda x: self._sensor_mag in x.values(),
                            sensor_data))
                        if data_entity:
                            self._last_update = data_entity[0][KEY_TIMESTAMP]
                            self._state = data_entity[0]['temp']
                        else:
                            _LOGGER.warning('No data_entity in update sensor by'
                                            ' id. Data:{}'.format(sensor_data))
                            return
                    else:
                        data_entity = sensor_data[0]
                        self._last_update = data_entity[KEY_TIMESTAMP]
                        self._state = data_entity[self._sensor_mag]
            except KeyError as e:
                _LOGGER.warning('KeyError: {}. No UPDATE OF {}'
                                .format(e, self._name))
                return
        if (self._round is not None) and (self._state is not None):
            self._state = round(self._state, self._round)
            _LOGGER.debug('New state in {} [{}]'.format(self._name,
                          self._state, self._last_update))


class EnerwebHeaterState(Entity):
    """Representation of the state of the heating system measured
    indirectly with enerweb temperature sensors."""

    def __init__(self, data_handler, sensor_supply, sensor_return, sensor_ref):
        """Initialize the sensor."""
        self._name = 'Calefacción'
        self._state = STATE_HEATING_UNKNOWN
        self._data = data_handler

        self._sensor_supply = sensor_supply
        self._sensor_return = sensor_return
        self._sensor_reference = sensor_ref
        self._supply_ant = deque([self._sensor_supply.state] * 3, 3)
        self.async_update()

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def state(self):
        """Return the state of the sensor."""
        return D_STATES_HEATING[self._state]

    @asyncio.coroutine
    def async_update(self):
        """Get the latest data and updates the states."""
        # self._data.async_update()
        if self._data.last_sensor_data is not None:
            t_imp = self._sensor_supply.state
            t_ret = self._sensor_return.state
            t_ref = self._sensor_reference.state
            state_ant = self._state
            t_imp_ant1, t_imp_ant2 = self._supply_ant[-1], self._supply_ant[-2]
            if (t_imp_ant1 is None) or(t_imp_ant2 is None):
                t_imp_ant1 = t_imp_ant2 = state_ant
            _LOGGER.debug('{}- UPDATE HEATER STATE: st_ant={}; IMP={}, RET={},'
                          ' REF={}; IMP1={}, IMP2={}'
                          .format(now(), state_ant, t_imp, t_ret,
                                  t_ref, t_imp_ant1, t_imp_ant2))

            if not t_imp or not t_ret or not t_ref:
                new_state = STATE_HEATING_UNKNOWN
            elif (t_imp - t_ref <= 7) or \
                    ((t_imp + t_ret) < 60 and (t_imp < 30)):
                new_state = STATE_HEATING_OFF
            # subida brusca desde Tº ~ ref
            elif (state_ant == STATE_HEATING_OFF) and \
                    (t_imp > t_ret) and (t_imp - t_imp_ant1 > 1):
                _LOGGER.debug('COLD START')
                new_state = STATE_HEATING_COLD_START
            # bajada brusca en ON
            elif ((state_ant > STATE_HEATING_OFF)
                  and (state_ant != STATE_HEATING_STOP) and
                      (t_imp - t_imp_ant1 < -.25) and
                      (t_imp - t_imp_ant2 < -.5)):
                _LOGGER.debug('HOT STOP')
                new_state = STATE_HEATING_STOP
            # subida brusca en ON
            elif (state_ant == STATE_HEATING_STOP) and \
                    (t_imp - t_imp_ant1 > .25) and (t_imp - t_imp_ant2 > .5):
                _LOGGER.debug('HOT RE-START')
                new_state = STATE_HEATING_RESTART
            elif (state_ant == STATE_HEATING_COLD_START) and \
                    (t_imp - t_imp_ant1 >= 0):
                _LOGGER.debug('ON AFTER COLD-START')
                new_state = STATE_HEATING_ON
            elif (state_ant == STATE_HEATING_RESTART) and \
                    (t_imp - t_imp_ant1 >= 0):
                _LOGGER.debug('ON AFTER RE-START')
                new_state = STATE_HEATING_ON
            # Confirmación de OFF (No Parada transitoria)
            elif (state_ant == STATE_HEATING_STOP) and \
                    (t_imp - t_ref < 25) and (t_imp_ant2 - t_imp > .5):
                new_state = STATE_HEATING_OFF
            elif (state_ant == STATE_HEATING_STOP) and \
                    (t_imp - t_imp_ant1 <= 2):
                new_state = STATE_HEATING_STOP
            elif (state_ant == STATE_HEATING_ON) and (t_imp - t_ref > 7):
                new_state = state_ant
            elif state_ant == STATE_HEATING_UNKNOWN:
                if (t_imp > t_ret) and (t_imp > t_ref + 7):
                    _LOGGER.info('INIT HASS WITH HEATING ON')
                    new_state = STATE_HEATING_ON
                else:
                    _LOGGER.info('INIT HASS WITH HEATING OFF')
                    new_state = STATE_HEATING_OFF
            else:
                new_state = state_ant
            self._state = new_state
            self._supply_ant.append(t_imp)


class EnerwebData(object):
    """Get the latest data for the enerweb platform."""

    def __init__(self, hass, enerweb_host, devices, timezone,
                 timeout=DEFAULT_TIMEOUT, mysql_user=None, mysql_pass=None):
        """Initialize the data handler object."""
        self.hass = hass
        self._host = enerweb_host
        self._site = None
        self._timezone = timezone
        self._timeout = timeout
        # self._scan_interval = timedelta(seconds=scan_interval)
        self._mysql_u = mysql_user
        self._mysql_p = mysql_pass
        # self._mysql_session = None
        self.path_database = URL_MASK_ENERWEB_GET_DATA_MYSQL.format(
            self._mysql_u, self._mysql_p, self._host)
        self._engine = None

        self._raw_data = None
        self._last_valid_request = None
        self._last_request_was_invalid = False
        self.last_sensor_data = None
        self._updating = False

        d_monitored_variables = {}
        for (sensor_type, sensor_mag, _, _, _, _) in devices:
            if sensor_type in d_monitored_variables:
                d_monitored_variables[sensor_type] += [sensor_mag]
            else:
                d_monitored_variables[sensor_type] = [sensor_mag]
        self._monitored_variables_mysql = d_monitored_variables

        async_track_point_in_utc_time(self.hass, self.async_update,
                                      now() + timedelta(seconds=3))
        async_track_time_interval(self.hass, self.async_update, SCAN_INTERVAL)

    @asyncio.coroutine
    def async_get_session(self):
        if self._engine is None:
            self._engine = yield from self.hass.async_add_job(
            partial(create_engine, self.path_database, echo=False))
            _LOGGER.debug('Created engine: {}'.format(self._engine))
        session = yield from self.hass.async_add_job(
            partial(sessionmaker, bind=self._engine))
        # return self._engine.connect()
        return session()

    # noinspection PyUnusedLocal
    @asyncio.coroutine
    def async_update(self, *args):
        """Update enerweb sensor data w/ mysql remote access."""
        if self._updating:
            _LOGGER.debug('no async_update --> is updating')
        else:
            self._updating = True
            try:
                # MYSQL Session:
                s = yield from self.async_get_session()
                # MYSQL QUERIES:
                last_data = {}
                for s_type, s_mags in self._monitored_variables_mysql.items():
                    table, cols_table = JSON_MYSQL_TRANSLATION[s_type]
                    cols_tsql = SQL_COLUMNS_TABLES[s_type]
                    onerow_table = table != 'measureds18b20'
                    n_last = 1 if onerow_table else len(s_mags)
                    task = yield from self.hass.async_add_job(
                        s.execute,
                        SQLMASK_SELECT_TABLE_LAST_VALUES.format(table, n_last))
                    last_values = yield from self.hass.async_add_job(
                        task.fetchall)
                    _LOGGER.debug('LAST VALUES ({}-{}) => {}'
                                  .format(s_type, table, last_values))
                    if onerow_table and last_values:
                        ts = last_values[0][cols_tsql.index(KEY_TIMESTAMP)]
                        last_data[s_type] = {KEY_TIMESTAMP: ts}
                        for mag in s_mags:
                            idx = cols_tsql.index(cols_table[mag])
                            last_data[s_type].update({mag: last_values[0][idx]})
                    elif last_values:  # ds18b20 multiple rows (1x sensor_id)
                        ts = last_values[-1][cols_tsql.index(KEY_TIMESTAMP)]
                        last_data[s_type] = {KEY_TIMESTAMP: ts}
                        idx_s_ids = cols_tsql.index(cols_table['temp'])
                        idx_values = cols_tsql.index('temperature')
                        for row in last_values:
                            last_data[s_type].update(
                                {row[idx_s_ids]: row[idx_values]})
                if last_data:
                    self.last_sensor_data = last_data
                    self._last_valid_request = now(self._timezone)
                    if self._last_request_was_invalid:
                        f_log = _LOGGER.info
                    else:
                        f_log = _LOGGER.debug
                    f_log('SQL_UPDATE last_data={} [{}]'
                          .format(last_data, self._last_valid_request))
                    self._last_request_was_invalid = False
                    # s.flush()
                else:
                    _LOGGER.warning('SQL_UPDATE NO LAST_DATA! => session={}'
                                    .format(s))
                    self._last_request_was_invalid = True
                    # self._mysql_session = None
            except (OperationalError, TimeoutError, InternalError,
                    OSError, SQLAlchemyError) as e:
                if not self._last_request_was_invalid:
                    _LOGGER.error('{}: {}'.format(e.__class__, e))
                # self._mysql_session = None
                self._last_request_was_invalid = True
                self._engine = None
            except Exception as e:
                if not self._last_request_was_invalid:
                    _LOGGER.error('UNKNOWN ERROR {} [{}] trying to update '
                                  'from SQL DB'.format(e, e.__class__))
                # self._mysql_session = None
                self._last_request_was_invalid = True
                self._engine = None
            self._updating = False
