# Copyright 2020 ACSONE SA
# Copyright 2020 Creu Blanca
# Copyright 2021 Camptocamp SA
# @author Simone Orsi <simahawk@gmail.com>
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).


import base64
import logging
import traceback
from io import StringIO

from odoo import _, exceptions, fields, models

from odoo.addons.component.exception import NoComponentError
from odoo.addons.queue_job.exception import RetryableJobError

from ..exceptions import EDIValidationError

_logger = logging.getLogger(__name__)


def _get_exception_msg():
    buff = StringIO()
    traceback.print_exc(file=buff)
    traceback_txt = buff.getvalue()
    buff.close()
    return traceback_txt


class EDIBackend(models.Model):
    """Generic backend to control EDI exchanges.

    Backends can be organized with types.

    The backend should be responsible for managing records.
    For each record it can generate or parse their values
    depending on their direction (incoming, outgoing)
    and send or receive them automatically depending on their state.
    """

    _name = "edi.backend"
    _description = "EDI Backend"
    _inherit = ["collection.base"]

    name = fields.Char(required=True)
    backend_type_id = fields.Many2one(
        string="EDI Backend type",
        comodel_name="edi.backend.type",
        required=True,
        ondelete="restrict",
    )
    backend_type_code = fields.Char(related="backend_type_id.code")
    output_sent_processed_auto = fields.Boolean(
        help="""
    Automatically set the record as processed after sending.
    Usecase: the web service you send the file to processes it on the fly.
    """
    )
    active = fields.Boolean(default=True)
    company_id = fields.Many2one("res.company", string="Company")

    def _get_component(self, exchange_record, key):
        record_conf = self._get_component_conf_for_record(exchange_record, key)
        # Load additional ctx keys if any
        collection = self
        # TODO: document/test this
        env_ctx = self._get_component_env_ctx(record_conf, key)
        collection = collection.with_context(**env_ctx)
        exchange_record = exchange_record.with_context(**env_ctx)
        work_ctx = {"exchange_record": exchange_record}
        # Inject work context from advanced settings
        work_ctx.update(record_conf.get("work_ctx", {}))
        # Model is not granted to be there
        model = exchange_record.model or self._name
        candidates = self._get_component_usage_candidates(exchange_record, key)
        match_attrs = self._component_match_attrs(exchange_record, key)
        return collection._find_component(
            model,
            candidates,
            work_ctx=work_ctx,
            **match_attrs,
        )

    def _get_component_env_ctx(self, record_conf, key):
        env_ctx = record_conf.get("env_ctx", {})
        # You can use `edi_session` down in the stack to control logics.
        env_ctx.update(dict(edi_framework_action=key))
        return env_ctx

    def _component_match_attrs(self, exchange_record, key):
        """Attributes that will be used to lookup components.

        They will be set in the work context and propagated to components.
        """
        return {
            "backend_type": self.backend_type_id.code,
            "exchange_type": exchange_record.type_id.code,
        }

    def _component_sort_key(self, component_class):
        """Determine the order of matched components.

        The order can be very important if your implementation
        allow generic / default components to be registered.
        """
        return (
            1 if component_class._backend_type else 0,
            1 if component_class._exchange_type else 0,
        )

    def _find_component(self, model, usage_candidates, safe=True, work_ctx=None, **kw):
        """Retrieve component for current backend.

        :param usage_candidates:
            list of usage to try by priority. 1st found, 1st returned
        :param safe: boolean, if true does not break if component is not found
        :param work_ctx: dictionary with work context params
        :param kw: keyword args to lookup for components (eg: usage)
        """
        component = None
        work_ctx = work_ctx or {}
        if "backend" not in work_ctx:
            work_ctx["backend"] = self
        with self.work_on(model, **work_ctx) as work:
            for usage in usage_candidates:
                components, c_work_ctx = work._matching_components(usage=usage, **kw)
                if not components:
                    continue
                # Sort components and pick the 1st one matching.
                # In this way we support generic components registration
                # and specific components registrations
                components = sorted(
                    components, key=lambda x: self._component_sort_key(x), reverse=True
                )
                component = components[0](c_work_ctx)
                _logger.debug("using component %s", component._name)
                break
        if not component and not safe:
            raise NoComponentError(
                "No component found matching any of: {}".format(usage_candidates)
            )
        return component or None

    def _get_component_usage_candidates(self, exchange_record, key):
        """Retrieve usage candidates for components."""
        # fmt:off
        base_usage = ".".join([
            exchange_record.direction,
            key,
        ])
        # fmt:on
        record_conf = self._get_component_conf_for_record(exchange_record, key)
        candidates = [record_conf["usage"]] if record_conf else []
        candidates += [
            base_usage,
        ]
        return candidates

    def _get_component_conf_for_record(self, exchange_record, key):
        settings = exchange_record.type_id.get_settings()
        return settings.get("components", {}).get(key, {})

    @property
    def exchange_record_model(self):
        return self.env["edi.exchange.record"]

    def create_record(self, type_code, values):
        """Create an exchange record for current backend.

        :param type_code: edi.exchange.type code
        :param values: edi.exchange.record values
        :return: edi.exchange.record record
        """
        self.ensure_one()
        _values = self._create_record_prepare_values(type_code, values)
        return self.exchange_record_model.create(_values)

    def _create_record_prepare_values(self, type_code, values):
        res = values.copy()  # do not pollute original dict
        exchange_type = self.env["edi.exchange.type"].search(
            self._get_exchange_type_domain(type_code), limit=1
        )
        assert exchange_type, f"Exchange type not found: {type_code}"
        res["type_id"] = exchange_type.id
        res["backend_id"] = self.id
        return res

    def _get_exchange_type_domain(self, code):
        return [
            ("code", "=", code),
            "|",
            ("backend_id", "=", self.id),
            "&",
            ("backend_type_id", "=", self.backend_type_id.id),
            ("backend_id", "=", False),
        ]

    def exchange_generate(self, exchange_record, store=True, force=False, **kw):
        """Generate output content for given exchange record.

        :param exchange_record: edi.exchange.record recordset
        :param store: store output on the record itself
        :param force: allow to re-generate the content
        :param kw: keyword args to be propagated to output generate handler
        """
        self.ensure_one()
        if force and exchange_record.exchange_file:
            # Remove file to regenerate
            exchange_record.exchange_file = False
        self._check_exchange_generate(exchange_record, force=force)
        output = self._exchange_generate(exchange_record, **kw)
        message = None
        encoding = exchange_record.type_id.encoding or "UTF-8"
        encoding_error_handler = (
            exchange_record.type_id.encoding_out_error_handler or "strict"
        )
        if output and store:
            if not isinstance(output, bytes):
                output = output.encode(encoding, errors=encoding_error_handler)
            exchange_record.update(
                {
                    "exchange_file": base64.b64encode(output),
                    "edi_exchange_state": "output_pending",
                }
            )
        if output:
            message = exchange_record._exchange_status_message("generate_ok")
            try:
                self._validate_data(exchange_record, output)
            except EDIValidationError:
                error = _get_exception_msg()
                state = "validate_error"
                message = exchange_record._exchange_status_message("validate_ko")
                exchange_record.update(
                    {"edi_exchange_state": state, "exchange_error": error}
                )
        exchange_record.notify_action_complete("generate", message=message)
        return message

    # TODO: unify to all other checkes that return something
    def _check_exchange_generate(self, exchange_record, force=False):
        exchange_record.ensure_one()
        if (
            exchange_record.edi_exchange_state != "new"
            and exchange_record.exchange_file
            and not force
        ):
            raise exceptions.UserError(
                _(
                    "Exchange record ID=%d is not in draft state "
                    "and has already an output value."
                )
                % exchange_record.id
            )
        if exchange_record.direction != "output":
            raise exceptions.UserError(
                _(
                    "Exchange record ID=%d is not an outgoing record, "
                    "cannot be generated"
                )
                % exchange_record.id
            )
        if exchange_record.exchange_file:
            raise exceptions.UserError(
                _("Exchange record ID=%d already has a file to process!")
                % exchange_record.id
            )

    def _exchange_generate(self, exchange_record, **kw):
        component = self._get_component(exchange_record, "generate")
        if component:
            return component.generate()
        raise NotImplementedError("No handler for `_exchange_generate`")

    # TODO: add tests
    def _validate_data(self, exchange_record, value=None, **kw):
        if exchange_record.direction == "input" and not exchange_record.exchange_file:
            if not exchange_record.type_id.allow_empty_files_on_receive:
                raise ValueError(
                    _("Empty files are not allowed for this exchange type")
                )

        component = self._get_component(exchange_record, "validate")
        if component:
            return component.validate(value)

    def exchange_send(self, exchange_record):
        """Send exchange file."""
        self.ensure_one()
        exchange_record.ensure_one()
        # In case already sent: skip sending and check the state
        check = self._output_check_send(exchange_record)
        if not check:
            return self._failed_output_check_send_msg()
        state = exchange_record.edi_exchange_state
        error = False
        message = None
        res = ""
        try:
            self._exchange_send(exchange_record)
            _logger.debug("%s sent", exchange_record.identifier)
        except self._send_retryable_exceptions() as err:
            error = _get_exception_msg()
            _logger.debug("%s send failed. To be retried.", exchange_record.identifier)
            raise RetryableJobError(
                error, **exchange_record._job_retry_params()
            ) from err
        except self._swallable_exceptions():
            if self.env.context.get("_edi_send_break_on_error"):
                raise
            error = _get_exception_msg()
            state = "output_error_on_send"
            message = exchange_record._exchange_status_message("send_ko")
            res = f"Error: {error}"
            _logger.debug(
                "%s send failed. Marked as errored.", exchange_record.identifier
            )
        else:
            # TODO: maybe the send handler should return desired message and state
            message = exchange_record._exchange_status_message("send_ok")
            error = None
            state = (
                "output_sent_and_processed"
                if self.output_sent_processed_auto
                else "output_sent"
            )
            res = message
        finally:
            exchange_record.write(
                {
                    "edi_exchange_state": state,
                    "exchange_error": error,
                    # FIXME: this should come from _compute_exchanged_on
                    # but somehow it's failing in send tests (in record tests it works).
                    "exchanged_on": fields.Datetime.now(),
                }
            )
        exchange_record.notify_action_complete("send", message=message)
        return res

    def _swallable_exceptions(self):
        # TODO: improve this list
        return (
            ValueError,
            FileNotFoundError,
            exceptions.UserError,
            exceptions.ValidationError,
        )

    def _send_retryable_exceptions(self):
        # IOError is a base class for all connection errors
        # OSError is a base class for all errors
        # when dealing w/ internal or external systems or filesystems
        return (IOError, OSError)

    def _output_check_send(self, exchange_record):
        if exchange_record.direction != "output":
            raise exceptions.UserError(
                _("Record ID=%d is not meant to be sent!") % exchange_record.id
            )
        if not exchange_record.exchange_file:
            raise exceptions.UserError(
                _("Record ID=%d has no file to send!") % exchange_record.id
            )
        return exchange_record.edi_exchange_state in [
            "output_pending",
            "output_error_on_send",
        ]

    def _exchange_send(self, exchange_record):
        component = self._get_component(exchange_record, "send")
        if component:
            return component.send()
        raise NotImplementedError("No handler for `_exchange_send`")

    def _cron_check_output_exchange_sync(self, **kw):
        for backend in self:
            backend._check_output_exchange_sync(**kw)

    def _check_output_exchange_sync(
        self, skip_send=False, skip_sent=True, record_ids=None
    ):
        """Lookup for pending output records and take care of them.

        First work on records that need output generation.
        Then work on records waiting for a state update.

        :param skip_send: only generate missing output.
        :param skip_sent: ignore records that were already sent.
        """
        # Generate output files
        new_records = self.exchange_record_model.search(
            self._output_new_records_domain(record_ids=record_ids)
        )
        _logger.info(
            "EDI Exchange output sync: found %d new records to process.",
            len(new_records),
        )
        for rec in new_records:
            job1 = rec.delayable().action_exchange_generate()
            if not skip_send:
                # Chain send job.
                # Raise prio to max to send the record out as fast as possible.
                job1.on_done(rec.delayable(priority=0).action_exchange_send())
            job1.delay()

        if skip_send:
            return
        pending_records = self.exchange_record_model.search(
            self._output_pending_records_domain(
                skip_sent=skip_sent, record_ids=record_ids
            )
        )
        _logger.info(
            "EDI Exchange output sync: found %d pending records to process.",
            len(pending_records),
        )
        for rec in pending_records:
            if rec.edi_exchange_state == "output_pending":
                rec.with_delay().action_exchange_send()
            else:
                # TODO: run in job as well?
                self._exchange_output_check_state(rec)

    def _output_new_records_domain(self, record_ids=None):
        """Domain for output records needing output content generation."""
        domain = [
            ("backend_id", "=", self.id),
            ("type_id.exchange_file_auto_generate", "=", True),
            ("type_id.direction", "=", "output"),
            ("edi_exchange_state", "=", "new"),
            ("exchange_file", "=", False),
        ]
        if record_ids:
            domain.append(("id", "in", record_ids))
        return domain

    def _output_pending_records_domain(self, skip_sent=True, record_ids=None):
        """Domain for pending output records.

        Records might be waiting to be sent or have errors or have ack to handle."""
        states = ("output_pending", "output_sent_and_error")
        if not skip_sent:
            # If you want to update sent records
            # you'll have to provide a `check` component.
            states += ("output_sent",)
        domain = [
            ("type_id.direction", "=", "output"),
            ("backend_id", "=", self.id),
            ("edi_exchange_state", "in", states),
        ]
        if record_ids:
            domain.append(("id", "in", record_ids))
        return domain

    def _exchange_output_check_state(self, exchange_record):
        component = self._get_component(exchange_record, "check")
        if component:
            return component.check()
        raise NotImplementedError("No handler for `_exchange_output_check_state`")

    def _exchange_process_check(self, exchange_record):
        if not exchange_record.direction == "input":
            raise exceptions.UserError(
                _("Record ID=%d is not meant to be processed") % exchange_record.id
            )
        if (
            not exchange_record.exchange_file
            and not exchange_record.type_id.allow_empty_files_on_receive
        ):
            raise exceptions.UserError(
                _("Record ID=%d has no file to process!") % exchange_record.id
            )
        return exchange_record.edi_exchange_state in [
            "input_received",
            "input_processed_error",
        ]

    def exchange_process(self, exchange_record):
        """Process an incoming document."""
        self.ensure_one()
        exchange_record.ensure_one()
        # In case already processed: skip processing and check the state
        check = self._exchange_process_check(exchange_record)
        if not check:
            return "Nothing to do. Likely already processed."
        old_state = state = exchange_record.edi_exchange_state
        error = False
        message = None
        try:
            res = self._exchange_process(exchange_record)
        except self._swallable_exceptions():
            if self.env.context.get("_edi_process_break_on_error"):
                raise
            error = _get_exception_msg()
            state = "input_processed_error"
            res = f"Error: {error}"
        else:
            error = None
            state = "input_processed"
        finally:
            exchange_record.write(
                {
                    "edi_exchange_state": state,
                    "exchange_error": error,
                    # FIXME: this should come from _compute_exchanged_on
                    # but somehow it's failing in send tests (in record tests it works).
                    "exchanged_on": fields.Datetime.now(),
                }
            )
            if (
                state == "input_processed_error"
                and old_state != "input_processed_error"
            ):
                exchange_record._notify_error("process_ko")
            elif state == "input_processed":
                exchange_record._notify_done()
        exchange_record.notify_action_complete("process", message=message)
        return res

    def _exchange_process(self, exchange_record):
        component = self._get_component(exchange_record, "process")
        if component:
            return component.process()
        raise NotImplementedError()

    def exchange_receive(self, exchange_record):
        """Retrieve an incoming document."""
        self.ensure_one()
        exchange_record.ensure_one()
        # In case already processed: skip processing and check the state
        check = self._exchange_receive_check(exchange_record)
        if not check:
            return "Nothing to do. Likely already received."
        state = exchange_record.edi_exchange_state
        error = False
        message = None
        content = None
        try:
            content = self._exchange_receive(exchange_record)
            # Ignore result of FileNotFoundError/OSError
            if content is not None:
                exchange_record._set_file_content(content)
                self._validate_data(exchange_record)
        except EDIValidationError:
            error = _get_exception_msg()
            state = "validate_error"
            message = exchange_record._exchange_status_message("validate_ko")
            res = f"Validation error: {error}"
        except self._swallable_exceptions():
            if self.env.context.get("_edi_receive_break_on_error"):
                raise
            error = _get_exception_msg()
            state = "input_receive_error"
            message = exchange_record._exchange_status_message("receive_ko")
            res = f"Input error: {error}"
        else:
            message = exchange_record._exchange_status_message("receive_ok")
            error = None
            state = "input_received"
            res = message
        finally:
            exchange_record.write(
                {
                    "edi_exchange_state": state,
                    "exchange_error": error,
                    # FIXME: this should come from _compute_exchanged_on
                    # but somehow it's failing in send tests (in record tests it works).
                    "exchanged_on": fields.Datetime.now(),
                }
            )
        exchange_record.notify_action_complete("receive", message=message)
        return res

    def _exchange_receive_check(self, exchange_record):
        # TODO: use `filtered_domain` + _input_pending_records_domain
        # and raise one single error
        # do the same for all the other check cases.
        if not exchange_record.direction == "input":
            raise exceptions.UserError(
                _("Record ID=%d is not meant to be processed") % exchange_record.id
            )
        return exchange_record.edi_exchange_state in [
            "input_pending",
            "input_receive_error",
        ]

    def _exchange_receive(self, exchange_record):
        component = self._get_component(exchange_record, "receive")
        if component:
            return component.receive()
        raise NotImplementedError()

    def _cron_check_input_exchange_sync(self, **kw):
        for backend in self:
            backend._check_input_exchange_sync(**kw)

    # TODO: add tests
    # TODO: consider splitting cron in 2 (1 for receiving, 1 for processing)
    def _check_input_exchange_sync(self, record_ids=None, **kw):
        """Lookup for pending input records and take care of them.

        First work on records that need to receive input.
        Then work on records waiting to be processed.
        """
        pending_records = self.exchange_record_model.search(
            self._input_pending_records_domain(record_ids=record_ids)
        )
        _logger.info(
            "EDI Exchange input sync: found %d pending records to receive.",
            len(pending_records),
        )
        for rec in pending_records:
            rec.with_delay().action_exchange_receive()

        pending_process_records = self.exchange_record_model.search(
            self._input_pending_process_records_domain(record_ids=record_ids)
        )
        _logger.info(
            "EDI Exchange input sync: found %d pending records to process.",
            len(pending_process_records),
        )
        for rec in pending_process_records:
            rec.with_delay().action_exchange_process()

    def _input_pending_records_domain(self, record_ids=None):
        domain = [
            ("backend_id", "=", self.id),
            ("type_id.direction", "=", "input"),
            ("edi_exchange_state", "=", "input_pending"),
            ("exchange_file", "=", False),
        ]
        if record_ids:
            domain.append(("id", "in", record_ids))
        return domain

    def _input_pending_process_records_domain(self, record_ids=None):
        states = ("input_received",)
        domain = [
            ("backend_id", "=", self.id),
            ("type_id.direction", "=", "input"),
            ("edi_exchange_state", "in", states),
        ]
        if record_ids:
            domain.append(("id", "in", record_ids))
        return domain

    def _find_existing_exchange_records(
        self, exchange_type, extra_domain=None, count_only=False
    ):
        domain = [
            ("backend_id", "=", self.id),
            ("type_id", "=", exchange_type.id),
        ] + extra_domain or []
        return self.env["edi.exchange.record"].search(domain, count=count_only)

    def action_view_exchanges(self):
        xmlid = "edi_oca.act_open_edi_exchange_record_view"
        action = self.env["ir.actions.act_window"]._for_xml_id(xmlid)
        action["context"] = {
            "search_default_backend_id": self.id,
            "default_backend_id": self.id,
            "default_backend_type_id": self.backend_type_id.id,
        }
        return action

    def action_view_exchange_types(self):
        xmlid = "edi_oca.act_open_edi_exchange_type_view"
        action = self.env["ir.actions.act_window"]._for_xml_id(xmlid)
        action["context"] = {
            "search_default_backend_id": self.id,
            "default_backend_id": self.id,
            "default_backend_type_id": self.backend_type_id.id,
        }
        return action

    def _is_valid_edi_action(self, action, raise_if_not=False):
        try:
            assert action in ("generate", "send", "process", "receive", "check")
            return True
        except AssertionError:
            if raise_if_not:
                raise
            return False

    def _failed_output_check_send_msg(self):
        return "Nothing to do. Likely already sent."
