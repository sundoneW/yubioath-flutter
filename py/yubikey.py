#!/usr/bin/env python
# -*- coding: utf-8 -*-

import json
import logging
import types
import ykman.logging_setup
from base64 import b32encode, b64decode
from binascii import a2b_hex, b2a_hex
from ykman.descriptor import (
    get_descriptors, FailedOpeningDeviceException)
from ykman.util import (TRANSPORT, parse_b32_key)
from ykman.driver_otp import YkpersError
from ykman.driver_ccid import APDUError
from ykman.oath import (
    ALGO, OATH_TYPE, OathController,
    CredentialData, Credential, Code, SW)
from ykman.otp import OtpController
from ykman.settings import Settings
from qr import qrparse, qrdecode


logger = logging.getLogger(__name__)


def as_json(f):
    def wrapped(*args):
        return json.dumps(f(*(json.loads(a) for a in args)))
    return wrapped


def cred_to_dict(cred):
    return {
        'key': cred.key.decode('utf8'),
        'issuer': cred.issuer,
        'name': cred.name,
        'oath_type': cred.oath_type.name,
        'period': cred.period,
        'touch': cred.touch
    }


def cred_from_dict(data):
    return Credential(
        data['key'].encode('utf-8'),
        OATH_TYPE[data['oath_type']],
        data['touch']
    )


def code_to_dict(code):
    return {
        'value': code.value,
        'valid_from': code.valid_from,
        'valid_to': min(code.valid_to, 9999999999)  # No Inf in JSON.
    } if code else None


def pair_to_dict(cred, code):
    return {
        'credential': cred_to_dict(cred),
        'code': code_to_dict(code)
    }


def credential_data_to_dict(credentialData):
    return {
        'secret': b32encode(credentialData.secret).decode(),
        'issuer': credentialData.issuer,
        'name': credentialData.name,
        'oath_type': credentialData.oath_type.name,
        'algorithm': credentialData.algorithm.name,
        'digits': credentialData.digits,
        'period': credentialData.period,
        'counter': credentialData.counter,
        'touch': credentialData.touch
    }


def success(result={}):
    result['success'] = True
    return result


def failure(err_id, result={}):
    result['success'] = False
    result['error_id'] = err_id
    return result


def unknown_failure(exception):
    return failure(str(exception))


def catch_error(f):
    def wrapped(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except YkpersError as e:
            if e.errno == 3:
                return failure('write error')
            if e.errno == 4:
                return failure('timeout')
            logger.error('Uncaught exception', exc_info=e)
            return unknown_failure(e)
        except FailedOpeningDeviceException:
            return failure('open_device_failed')
        except APDUError as e:
            if e.sw == SW.SECURITY_CONDITION_NOT_SATISFIED:
                return failure('access_denied')
            raise
        except Exception as e:
            if str(e) == 'Incorrect padding':
                return failure('incorrect_padding')
            logger.error('Uncaught exception', exc_info=e)
            return unknown_failure(e)
    return wrapped


class OathContextManager(object):
    def __init__(self, dev):
        self._dev = dev

    def __enter__(self):
        return OathController(self._dev.driver)

    def __exit__(self, exc_type, exc_value, traceback):
        self._dev.close()


class OtpContextManager(object):
    def __init__(self, dev):
        self._dev = dev

    def __enter__(self):
        return OtpController(self._dev.driver)

    def __exit__(self, exc_type, exc_value, traceback):
        self._dev.close()


class Controller(object):

    _descs = []
    _desc_fps = []
    _current_desc = None
    _devices = []
    _current_key = None
    _keys = []

    def __init__(self):
        self.settings = Settings('oath')

        # Wrap all args and return values as JSON.
        for f in dir(self):
            if not f.startswith('_'):
                func = getattr(self, f)
                if isinstance(func, types.MethodType):
                    setattr(self, f, as_json(catch_error(func)))

    def _open_oath(self):
        return OathContextManager(
            self._current_desc.open_device(TRANSPORT.CCID))

    def _open_otp(self):
        return OtpContextManager(
            self._current_desc.open_device(TRANSPORT.OTP))

    def _update_desc_fps(self):
        descs = get_descriptors()
        self._descs = descs
        self._desc_fps = [desc.fingerprint for desc in descs]
        # TODO: Don't always select the first descriptor, be smarter.
        self._current_desc = descs[0] if descs else None

    def refresh_devices(self, otp_mode=False):
        old_desc_fps = self._desc_fps
        self._update_desc_fps()
        descs_changed = (old_desc_fps != self._desc_fps)
        if descs_changed:
            self._devices = []
            for desc in self._descs:
                dev = desc.open_device(TRANSPORT.OTP if otp_mode else TRANSPORT.CCID)
                self._devices.append({
                    'name': dev.device_name,
                    'version': dev.version,
                    'serial': dev.serial,
                    'fingerprint': desc.fingerprint
                })
        return success({'devices': self._devices})

    def ccid_calculate_all(self, timestamp):
        with self._open_oath() as oath_controller:
            self._unlock(oath_controller)
            entries = oath_controller.calculate_all(timestamp)
            return success(
                {
                    'entries': [
                        pair_to_dict(
                            cred, code) for (
                                cred, code) in entries if not cred.is_hidden]
                }
            )

    def ccid_calculate(self, credential, timestamp):
        with self._open_oath() as oath_controller:
            self._unlock(oath_controller)
            code = oath_controller.calculate(
                cred_from_dict(credential), timestamp)
            return success({
                'credential': credential,
                'code': code_to_dict(code)
            })

    def ccid_add_credential(
            self, name, secret, issuer, oath_type,
            algo, digits, period, touch):
        secret = parse_b32_key(secret)
        with self._open_oath() as oath_controller:
            try:
                self._unlock(oath_controller)
                oath_controller.put(CredentialData(
                    secret, issuer, name, OATH_TYPE[oath_type], ALGO[algo],
                    int(digits), int(period), 0, touch
                ))
            except APDUError as e:
                # NEO doesn't return a no space error if full,
                # but a command aborted error. Assume it's because of
                # no space in this context.
                if e.sw in (SW.NO_SPACE, SW.COMMAND_ABORTED):
                    return failure('no_space')
                else:
                    raise
            return success()

    def ccid_validate(self, password, remember=False):
        with self._open_oath() as oath_controller:
            key = oath_controller.derive_key(password)
            try:
                oath_controller.validate(key)
                self._current_key = key
                if remember:
                    keys = self.settings.setdefault('keys', {})
                    keys[oath_controller.id] = b2a_hex(
                        self._current_key).decode()
                    self.settings.write()
                return success()
            except APDUError as e:
                if e.sw == SW.INCORRECT_PARAMETERS:
                    return failure('validate_failed')

    def _otp_get_code_or_touch(self, slot, digits, timestamp):
        code = None
        touch = False
        with self._open_otp() as otp_controller:
            try:
                code = otp_controller.calculate(slot, challenge=timestamp, totp=True, digits=int(digits), wait_for_touch=False)
            except YkpersError as e:
                if e.errno == 11: # Operation would block, touch credential
                    touch = True
                else:
                    raise
            return code, touch

    def otp_calculate_all(self, slot1_in_use, slot1_digits, slot2_in_use, slot2_digits, timestamp):
        valid_from = timestamp - (timestamp % 30)
        valid_to = valid_from + 30
        entries = []
        if slot1_in_use:
            code, touch = self._otp_get_code_or_touch(1, slot1_digits, timestamp)
            entries.append({
                'credential': cred_to_dict(Credential(str("Slot 1").encode('utf-8'), OATH_TYPE.TOTP, touch)),
                'code': code_to_dict(Code(code, valid_from, valid_to)) if code else None
            })
        if slot2_in_use:
            code, touch = self._otp_get_code_or_touch(2, slot2_digits, timestamp)
            entries.append({
                'credential': cred_to_dict(Credential(str("Slot 2").encode('utf-8'), OATH_TYPE.TOTP, touch)),
                'code': code_to_dict(Code(code, valid_from, valid_to)) if code else None
            })
        return success({'entries': entries})

    def otp_calculate(self, slot, digits, credential, timestamp):
        valid_from = timestamp - (timestamp % 30)
        valid_to = valid_from + 30
        with self._open_otp() as otp_controller:
            code = otp_controller.calculate(slot, challenge=timestamp, totp=True, digits=int(digits), wait_for_touch=True)
            return success({
                'credential': credential,
                'code': code_to_dict(Code(code, valid_from, valid_to))
            })

    def _unlock(self, controller):
        if controller.locked:
            keys = self.settings.get('keys', {})
            if self._current_key is not None:
                controller.validate(self._current_key)
            elif controller.id in keys:
                controller.validate(a2b_hex(keys[controller.id]))
            else:
                return failure('failed_to_unlock_key')

    def ccid_delete_credential(self, credential):
        with self._open_oath() as oath_controller:
            self._unlock(oath_controller)
            oath_controller.delete(cred_from_dict(credential))
            return success()

    def ccid_reset(self):
        with self._open_oath() as oath_controller:
            oath_controller.reset()
            return success()

    def ccid_set_password(self, new_password, remember=False):
        with self._open_oath() as oath_controller:
            self._unlock(oath_controller)
            keys = self.settings.setdefault('keys', {})
            self._current_key = oath_controller.set_password(new_password)
            if remember:
                keys[oath_controller.id] = b2a_hex(self._current_key).decode()
            elif oath_controller.id in keys:
                del keys[oath_controller.id]
            self.settings.write()
            return success()

    def parse_qr(self, screenshot):
        data = b64decode(screenshot['data'])
        image = PixelImage(data, screenshot['width'], screenshot['height'])
        for qr in qrparse.parse_qr_codes(image, 2):
            return success(
                credential_data_to_dict(
                    CredentialData.from_uri(qrdecode.decode_qr_data(qr))))
        return failure('no_credential_found')


class PixelImage(object):

    def __init__(self, data, width, height):
        self.data = data
        self.width = width
        self.height = height

    def get_line(self, line_number):
        return self.data[
            self.width * line_number:self.width * (line_number + 1)]


controller = None


def init_with_logging(log_level, log_file=None):
    logging_setup = as_json(ykman.logging_setup.setup)
    logging_setup(log_level, log_file)
    init()


def init():
    global controller
    controller = Controller()
