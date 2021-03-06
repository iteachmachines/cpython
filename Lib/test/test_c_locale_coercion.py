# Tests the attempted automatic coercion of the C locale to a UTF-8 locale

import unittest
import os
import sys
import sysconfig
import shutil
import subprocess
from collections import namedtuple

import test.support
from test.support.script_helper import (
    run_python_until_end,
    interpreter_requires_environment,
)

# In order to get the warning messages to match up as expected, the candidate
# order here must much the target locale order in Python/pylifecycle.c
_C_UTF8_LOCALES = ("C.UTF-8", "C.utf8", "UTF-8")

# There's no reliable cross-platform way of checking locale alias
# lists, so the only way of knowing which of these locales will work
# is to try them with locale.setlocale(). We do that in a subprocess
# to avoid altering the locale of the test runner.
def _set_locale_in_subprocess(locale_name):
    cmd_fmt = "import locale; print(locale.setlocale(locale.LC_CTYPE, '{}'))"
    cmd = cmd_fmt.format(locale_name)
    result, py_cmd = run_python_until_end("-c", cmd, __isolated=True)
    return result.rc == 0

_EncodingDetails = namedtuple("EncodingDetails",
                              "fsencoding stdin_info stdout_info stderr_info")

class EncodingDetails(_EncodingDetails):
    CHILD_PROCESS_SCRIPT = ";".join([
        "import sys",
        "print(sys.getfilesystemencoding())",
        "print(sys.stdin.encoding + ':' + sys.stdin.errors)",
        "print(sys.stdout.encoding + ':' + sys.stdout.errors)",
        "print(sys.stderr.encoding + ':' + sys.stderr.errors)",
    ])

    @classmethod
    def get_expected_details(cls, expected_fsencoding):
        """Returns expected child process details for a given encoding"""
        _stream = expected_fsencoding + ":{}"
        # stdin and stdout should use surrogateescape either because the
        # coercion triggered, or because the C locale was detected
        stream_info = 2*[_stream.format("surrogateescape")]
        # stderr should always use backslashreplace
        stream_info.append(_stream.format("backslashreplace"))
        return dict(cls(expected_fsencoding, *stream_info)._asdict())

    @staticmethod
    def _handle_output_variations(data):
        """Adjust the output to handle platform specific idiosyncrasies

        * Some platforms report ASCII as ANSI_X3.4-1968
        * Some platforms report ASCII as US-ASCII
        * Some platforms report UTF-8 instead of utf-8
        """
        data = data.replace(b"ANSI_X3.4-1968", b"ascii")
        data = data.replace(b"US-ASCII", b"ascii")
        data = data.lower()
        return data

    @classmethod
    def get_child_details(cls, env_vars):
        """Retrieves fsencoding and standard stream details from a child process

        Returns (encoding_details, stderr_lines):

        - encoding_details: EncodingDetails for eager decoding
        - stderr_lines: result of calling splitlines() on the stderr output

        The child is run in isolated mode if the current interpreter supports
        that.
        """
        result, py_cmd = run_python_until_end(
            "-c", cls.CHILD_PROCESS_SCRIPT,
            __isolated=True,
            **env_vars
        )
        if not result.rc == 0:
            result.fail(py_cmd)
        # All subprocess outputs in this test case should be pure ASCII
        adjusted_output = cls._handle_output_variations(result.out)
        stdout_lines = adjusted_output.decode("ascii").rstrip().splitlines()
        child_encoding_details = dict(cls(*stdout_lines)._asdict())
        stderr_lines = result.err.decode("ascii").rstrip().splitlines()
        return child_encoding_details, stderr_lines


class _ChildProcessEncodingTestCase(unittest.TestCase):
    # Base class to check for expected encoding details in a child process

    def _check_child_encoding_details(self,
                                      env_vars,
                                      expected_fsencoding,
                                      expected_warning):
        """Check the C locale handling for the given process environment

        Parameters:
            expected_fsencoding: the encoding the child is expected to report
            allow_c_locale: setting to use for PYTHONALLOWCLOCALE
              None: don't set the variable at all
              str: the value set in the child's environment
        """
        result = EncodingDetails.get_child_details(env_vars)
        encoding_details, stderr_lines = result
        self.assertEqual(encoding_details,
                         EncodingDetails.get_expected_details(
                             expected_fsencoding))
        self.assertEqual(stderr_lines, expected_warning)

# Details of the shared library warning emitted at runtime
LIBRARY_C_LOCALE_WARNING = (
    "Python runtime initialized with LC_CTYPE=C (a locale with default ASCII "
    "encoding), which may cause Unicode compatibility problems. Using C.UTF-8, "
    "C.utf8, or UTF-8 (if available) as alternative Unicode-compatible "
    "locales is recommended."
)

@unittest.skipUnless(sysconfig.get_config_var("PY_WARN_ON_C_LOCALE"),
                     "C locale runtime warning disabled at build time")
class LocaleWarningTests(_ChildProcessEncodingTestCase):
    # Test warning emitted when running in the C locale

    def test_library_c_locale_warning(self):
        self.maxDiff = None
        for locale_to_set in ("C", "POSIX", "invalid.ascii"):
            var_dict = {
                "LC_ALL": locale_to_set
            }
            with self.subTest(forced_locale=locale_to_set):
                self._check_child_encoding_details(var_dict,
                                                   "ascii",
                                                   [LIBRARY_C_LOCALE_WARNING])

# Details of the CLI locale coercion warning emitted at runtime
CLI_COERCION_WARNING_FMT = (
    "Python detected LC_CTYPE=C: LC_CTYPE coerced to {} (set another locale "
    "or PYTHONCOERCECLOCALE=0 to disable this locale coercion behavior)."
)

class _LocaleCoercionTargetsTestCase(_ChildProcessEncodingTestCase):
    # Base class for test cases that rely on coercion targets being defined

    available_targets = []
    targets_required = True

    @classmethod
    def setUpClass(cls):
        first_target_locale = None
        available_targets = cls.available_targets
        # Find the target locales available in the current system
        for target_locale in _C_UTF8_LOCALES:
            if _set_locale_in_subprocess(target_locale):
                available_targets.append(target_locale)
                if first_target_locale is None:
                    first_target_locale = target_locale
        if cls.targets_required and not available_targets:
            raise unittest.SkipTest("No C-with-UTF-8 locale available")
        # Expect coercion to use the first available locale
        warning_msg = CLI_COERCION_WARNING_FMT.format(first_target_locale)
        cls.EXPECTED_COERCION_WARNING = warning_msg


class LocaleConfigurationTests(_LocaleCoercionTargetsTestCase):
    # Test explicit external configuration via the process environment

    def test_external_target_locale_configuration(self):
        # Explicitly setting a target locale should give the same behaviour as
        # is seen when implicitly coercing to that target locale
        self.maxDiff = None

        expected_warning = []
        expected_fsencoding = "utf-8"

        base_var_dict = {
            "LANG": "",
            "LC_CTYPE": "",
            "LC_ALL": "",
        }
        for env_var in ("LANG", "LC_CTYPE"):
            for locale_to_set in self.available_targets:
                with self.subTest(env_var=env_var,
                                  configured_locale=locale_to_set):
                    var_dict = base_var_dict.copy()
                    var_dict[env_var] = locale_to_set
                    self._check_child_encoding_details(var_dict,
                                                       expected_fsencoding,
                                                       expected_warning)



@test.support.cpython_only
@unittest.skipUnless(sysconfig.get_config_var("PY_COERCE_C_LOCALE"),
                     "C locale coercion disabled at build time")
class LocaleCoercionTests(_LocaleCoercionTargetsTestCase):
    # Test implicit reconfiguration of the environment during CLI startup

    def _check_c_locale_coercion(self, expected_fsencoding, coerce_c_locale):
        """Check the C locale handling for various configurations

        Parameters:
            expected_fsencoding: the encoding the child is expected to report
            allow_c_locale: setting to use for PYTHONALLOWCLOCALE
              None: don't set the variable at all
              str: the value set in the child's environment
        """

        # Check for expected warning on stderr if C locale is coerced
        self.maxDiff = None

        expected_warning = []
        if coerce_c_locale != "0":
            expected_warning.append(self.EXPECTED_COERCION_WARNING)

        base_var_dict = {
            "LANG": "",
            "LC_CTYPE": "",
            "LC_ALL": "",
        }
        for env_var in ("LANG", "LC_CTYPE"):
            for locale_to_set in ("", "C", "POSIX", "invalid.ascii"):
                with self.subTest(env_var=env_var,
                                  nominal_locale=locale_to_set,
                                  PYTHONCOERCECLOCALE=coerce_c_locale):
                    var_dict = base_var_dict.copy()
                    var_dict[env_var] = locale_to_set
                    if coerce_c_locale is not None:
                        var_dict["PYTHONCOERCECLOCALE"] = coerce_c_locale
                    self._check_child_encoding_details(var_dict,
                                                       expected_fsencoding,
                                                       expected_warning)

    def test_test_PYTHONCOERCECLOCALE_not_set(self):
        # This should coerce to the first available target locale by default
        self._check_c_locale_coercion("utf-8", coerce_c_locale=None)

    def test_PYTHONCOERCECLOCALE_not_zero(self):
        # *Any* string other that "0" is considered "set" for our purposes
        # and hence should result in the locale coercion being enabled
        for setting in ("", "1", "true", "false"):
            self._check_c_locale_coercion("utf-8", coerce_c_locale=setting)

    def test_PYTHONCOERCECLOCALE_set_to_zero(self):
        # The setting "0" should result in the locale coercion being disabled
        self._check_c_locale_coercion("ascii", coerce_c_locale="0")


def test_main():
    test.support.run_unittest(
        LocaleConfigurationTests,
        LocaleCoercionTests,
        LocaleWarningTests
    )
    test.support.reap_children()

if __name__ == "__main__":
    test_main()
