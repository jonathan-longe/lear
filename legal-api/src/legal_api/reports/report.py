# Copyright © 2019 Province of British Columbia
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance with
# the License. You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
# an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
# specific language governing permissions and limitations under the License.
"""Produces a PDF output based on templates and JSON messages."""
import base64
import copy
import json
import os
from contextlib import suppress
from datetime import datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Final

import pycountry
import requests
from flask import current_app, jsonify

from legal_api.core.meta.filing import FILINGS
from legal_api.models import Business, CorpType, Document, Filing, PartyRole
from legal_api.models.business import ASSOCIATION_TYPE_DESC
from legal_api.reports.registrar_meta import RegistrarInfo
from legal_api.services import MinioService, VersionedBusinessDetailsService
from legal_api.utils.auth import jwt
from legal_api.utils.legislation_datetime import LegislationDatetime


OUTPUT_DATE_FORMAT: Final = '%B %-d, %Y'


class Report:  # pylint: disable=too-few-public-methods, too-many-lines
    # TODO review pylint warning and alter as required
    """Service to create report outputs."""

    def __init__(self, filing):
        """Create the Report instance."""
        self._filing = filing
        self._business = None
        self._report_key = None
        self._report_date_time = LegislationDatetime.now()

    def get_pdf(self, report_type=None):
        """Render a pdf for the report."""
        self._report_key = report_type if report_type else self._filing.filing_type
        if self._report_key in ReportMeta.static_reports:
            return self._get_static_report()
        return self._get_report()

    def _get_static_report(self):
        document_type = ReportMeta.static_reports[self._report_key]['documentType']
        document: Document = self._filing.documents.filter(Document.type == document_type).first()
        response = MinioService.get_file(document.file_key)
        return response.data, response.status

    def _get_report(self):
        if self._filing.business_id:
            self._business = Business.find_by_internal_id(self._filing.business_id)
            Report._populate_business_info_to_filing(self._filing, self._business)
        if self._report_key == 'alteration':
            self._report_key = 'alterationNotice'
        headers = {
            'Authorization': 'Bearer {}'.format(jwt.get_token_auth_header()),
            'Content-Type': 'application/json'
        }
        data = {
            'reportName': self._get_report_filename(),
            'template': "'" + base64.b64encode(bytes(self._get_template(), 'utf-8')).decode() + "'",
            'templateVars': self._get_template_data()
        }
        response = requests.post(url=current_app.config.get('REPORT_SVC_URL'), headers=headers, data=json.dumps(data))

        if response.status_code != HTTPStatus.OK:
            return jsonify(message=str(response.content)), response.status_code
        return response.content, response.status_code

    def _get_report_filename(self):
        filing_date = str(self._filing.filing_date)[:19]
        legal_entity_number = self._business.identifier if self._business else \
            self._filing.filing_json['filing'].get('business', {}).get('identifier', '')
        description = ReportMeta.reports[self._report_key]['filingDescription']
        return '{}_{}_{}.pdf'.format(legal_entity_number, filing_date, description).replace(' ', '_')

    def _get_template(self):
        try:
            template_path = current_app.config.get('REPORT_TEMPLATE_PATH')
            template_code = Path(f'{template_path}/{self._get_template_filename()}').read_text()
            # substitute template parts
            template_code = self._substitute_template_parts(template_code)
        except Exception as err:
            current_app.logger.error(err)
            raise err
        return template_code

    @staticmethod
    def _substitute_template_parts(template_code):
        """Substitute template parts in main template.

        Template parts are marked by [[partname.html]] in templates.

        This functionality is restricted by:
        - markup must be exactly [[partname.html]] and have no extra spaces around file name
        - template parts can only be one level deep, ie: this rudimentary framework does not handle nested template
        parts. There is no recursive search and replace.

        :param template_code: string
        :return: template_code string, modified.
        """
        template_path = current_app.config.get('REPORT_TEMPLATE_PATH')
        template_parts = [
            'bc-annual-report/legalObligations',
            'bc-address-change/addresses',
            'bc-director-change/directors',
            'common/certificateFooter',
            'common/certificateLogo',
            'common/certificateRegistrarSignature',
            'common/certificateSeal',
            'common/certificateStyle',
            'common/addresses',
            'common/shareStructure',
            'common/correctedOnCertificate',
            'common/style',
            'common/businessDetails',
            'common/directors',
            'common/completingParty',
            'correction/businessDetails',
            'correction/addresses',
            'correction/directors',
            'change-of-registration/legal-name',
            'change-of-registration/nature-of-business',
            'change-of-registration/addresses',
            'change-of-registration/proprietor',
            'change-of-registration/completingParty',
            'change-of-registration/partner',
            'incorporation-application/benefitCompanyStmt',
            'incorporation-application/completingParty',
            'incorporation-application/effectiveDate',
            'incorporation-application/incorporator',
            'incorporation-application/nameRequest',
            'incorporation-application/cooperativeAssociationType',
            'restoration-application/nameRequest',
            'restoration-application/legalName',
            'restoration-application/legalNameDissolution',
            'restoration-application/approvalType',
            'restoration-application/applicant',
            'restoration-application/expiry',
            'registration/nameRequest',
            'registration/addresses',
            'registration/completingParty',
            'registration/party',
            'registration-statement/party',
            'registration-statement/business-info',
            'registration-statement/completingParty',
            'common/statement',
            'common/benefitCompanyStmt',
            'dissolution/custodianOfRecords',
            'dissolution/dissolutionStatement',
            'dissolution/firmsDissolutionDate',
            'notice-of-articles/directors',
            'notice-of-articles/restrictions',
            'common/resolutionDates',
            'alteration-notice/businessTypeChange',
            'alteration-notice/legalNameChange',
            'alteration-notice/statement',
            'common/effectiveDate',
            'common/nameTranslation',
            'alteration-notice/companyProvisions',
            'special-resolution/resolution',
            'addresses',
            'certification',
            'directors',
            'dissolution',
            'footer',
            'legalNameChange',
            'logo',
            'macros',
            'style'
        ]

        # substitute template parts - marked up by [[filename]]
        for template_part in template_parts:
            template_part_code = Path(f'{template_path}/template-parts/{template_part}.html').read_text()
            template_code = template_code.replace('[[{}.html]]'.format(template_part), template_part_code)

        return template_code

    def _get_template_filename(self):
        if ReportMeta.reports[self._report_key].get('hasDifferentTemplates', False):
            # Get template specific to legal type
            specific_template = ReportMeta.reports[self._report_key].get(self._business.legal_type, None)
            # Fallback to default if specific template not found
            file_name = specific_template['fileName'] if specific_template else \
                ReportMeta.reports[self._report_key]['default']['fileName']
        else:
            file_name = ReportMeta.reports[self._report_key]['fileName']
        return '{}.html'.format(file_name)

    def _get_template_data(self):  # pylint: disable=too-many-branches
        if self._report_key in ['noticeOfArticles', 'amendedRegistrationStatement', 'correctedRegistrationStatement']:
            filing = VersionedBusinessDetailsService.get_company_details_revision(self._filing.id, self._business.id)
            self._format_noa_data(filing)
        else:
            filing = copy.deepcopy(self._filing.filing_json['filing'])
            filing['header']['filingId'] = self._filing.id
            filing['header']['status'] = self._filing.status
            if self._report_key == 'incorporationApplication':
                self._format_incorporation_data(filing)
            elif self._report_key == 'specialResolution':
                self._format_special_resolution(filing)
            elif self._report_key == 'alterationNotice':
                self._format_alteration_data(filing)
            elif self._report_key == 'registration':
                self._format_registration_data(filing)
            elif self._report_key == 'changeOfRegistration':
                self._format_change_of_registration_data(filing, 'changeOfRegistration')
            elif self._report_key == 'certificateOfNameChange':
                self._format_name_change_data(filing)
            elif self._report_key == 'certificateOfRestoration':
                self._format_certificate_of_restoration_data(filing)
            elif self._report_key == 'restoration':
                self._format_restoration_data(filing)
            else:
                # set registered office address from either the COA filing or status quo data in AR filing
                with suppress(KeyError):
                    self._set_addresses(filing)
                # set director list from either the COD filing or status quo data in AR filing
                with suppress(KeyError):
                    self._set_directors(filing)

            if self._report_key == 'transition':
                self._format_transition_data(filing)

            if self._report_key == 'dissolution':
                filing['dissolution']['dissolution_date_str'] = \
                    datetime.fromisoformat(filing['dissolution']['dissolutionDate']).strftime(OUTPUT_DATE_FORMAT)
                self._format_directors(filing['dissolution']['parties'])
                filing['parties'] = filing['dissolution']['parties']

            # since we reset _report_key with correction type
            if filing['header']['name'] == 'correction':
                if self._business.legal_type in ['SP', 'GP']:
                    self._format_change_of_registration_data(filing, 'correction')
                else:
                    self._format_correction_data(filing)

            filing['meta_data'] = self._filing.meta_data or {}

        filing['header']['reportType'] = self._report_key

        self._set_dates(filing)
        self._set_description(filing)
        self._set_tax_id(filing)
        self._set_meta_info(filing)
        self._set_registrar_info(filing)
        self._set_completing_party(filing)
        return filing

    def _set_completing_party(self, filing):
        completing_party_role = PartyRole.get_party_roles_by_filing(
            self._filing.id, datetime.utcnow(), PartyRole.RoleTypes.COMPLETING_PARTY.value)
        if completing_party_role:
            filing['completingParty'] = completing_party_role[0].party.json
            with suppress(KeyError):
                self._format_address(filing['completingParty']['deliveryAddress'])
            with suppress(KeyError):
                self._format_address(filing['completingParty']['mailingAddress'])

    def _set_registrar_info(self, filing):
        if filing.get('correction'):
            original_filing = Filing.find_by_id(filing.get('correction').get('correctedFilingId'))
            original_registrar = {**RegistrarInfo.get_registrar_info(original_filing.effective_date)}
            filing['registrarInfo'] = original_registrar
            current_registrar = {**RegistrarInfo.get_registrar_info(self._filing.effective_date)}
            if original_registrar['name'] != current_registrar['name']:
                filing['currentRegistrarInfo'] = current_registrar
        elif filing.get('annualReport'):
            # effective_date in annualReport will be ar_date or agm_date, which could be in past.
            filing['registrarInfo'] = {**RegistrarInfo.get_registrar_info(self._filing.filing_date)}
        else:
            filing['registrarInfo'] = {**RegistrarInfo.get_registrar_info(self._filing.effective_date)}

    def _set_tax_id(self, filing):
        if self._business and self._business.tax_id:
            filing['taxId'] = self._business.tax_id

    def _set_description(self, filing):
        legal_type = self._filing.filing_json['filing'].get('business', {}).get('legalType', 'NA')
        filing['numberedDescription'] = Business.BUSINESSES.get(legal_type, {}).get('numberedDescription')

        corp_type = CorpType.find_by_id(legal_type)
        filing['entityDescription'] = corp_type.full_desc

        act = {
            Business.LegalTypes.BCOMP.value: 'Business Corporations Act',
            Business.LegalTypes.COMP.value: 'Business Corporations Act',
            Business.LegalTypes.BC_CCC.value: 'Business Corporations Act',
            Business.LegalTypes.BC_ULC_COMPANY.value: 'Business Corporations Act',
            Business.LegalTypes.COOP.value: 'Cooperative Association Act',
            Business.LegalTypes.SOLE_PROP.value: 'Partnership Act',
            Business.LegalTypes.PARTNERSHIP.value: 'Partnership Act'
        }  # This could be the legislation column from CorpType. Yet to discuss.
        filing['entityAct'] = act.get(legal_type, 'Business Corporations Act')

    def _set_dates(self, filing):
        # Filing Date
        filing_datetime = LegislationDatetime.as_legislation_timezone(self._filing.filing_date)
        filing['filing_date_time'] = LegislationDatetime.format_as_report_string(filing_datetime)
        # Effective Date
        effective_date = filing_datetime if self._filing.effective_date is None \
            else LegislationDatetime.as_legislation_timezone(self._filing.effective_date)
        filing['effective_date_time'] = LegislationDatetime.format_as_report_string(effective_date)
        filing['effective_date'] = effective_date.strftime(OUTPUT_DATE_FORMAT)
        # Recognition Date
        if self._business:
            recognition_datetime = LegislationDatetime.as_legislation_timezone(self._business.founding_date)
            filing['recognition_date_time'] = LegislationDatetime.format_as_report_string(recognition_datetime)
            filing['recognition_date_utc'] = recognition_datetime.strftime(OUTPUT_DATE_FORMAT)
            if self._business.start_date:
                filing['start_date_utc'] = self._business.start_date.strftime(OUTPUT_DATE_FORMAT)
        # For Annual Report - Set AGM date as the effective date
        if self._filing.filing_type == 'annualReport':
            agm_date_str = filing.get('annualReport', {}).get('annualGeneralMeetingDate', None)
            if agm_date_str:
                agm_date = datetime.fromisoformat(agm_date_str)
                filing['agm_date'] = agm_date.strftime(OUTPUT_DATE_FORMAT)
                # for AR, the effective date is the AGM date
                filing['effective_date'] = agm_date.strftime(OUTPUT_DATE_FORMAT)
            else:
                filing['agm_date'] = 'No AGM'
        if filing.get('correction'):
            original_filing = Filing.find_by_id(filing.get('correction').get('correctedFilingId'))
            original_filing_datetime = LegislationDatetime.as_legislation_timezone(original_filing.filing_date)
            filing['original_filing_date_time'] = LegislationDatetime.format_as_report_string(original_filing_datetime)
        filing['report_date_time'] = LegislationDatetime.format_as_report_string(self._report_date_time)
        filing['report_date'] = self._report_date_time.strftime(OUTPUT_DATE_FORMAT)

    def _set_directors(self, filing):
        if filing.get('changeOfDirectors'):
            filing['listOfDirectors'] = filing['changeOfDirectors']
        else:
            filing['listOfDirectors'] = {
                'directors': filing['annualReport']['directors']
            }
        # create helper lists of appointed and ceased directors
        directors = self._format_directors(filing['listOfDirectors']['directors'])
        filing['listOfDirectors']['directorsAppointed'] = [el for el in directors if 'appointed' in el['actions']]
        filing['listOfDirectors']['directorsCeased'] = [el for el in directors if 'ceased' in el['actions']]

    def _format_directors(self, directors):
        for director in directors:
            with suppress(KeyError):
                self._format_address(director['deliveryAddress'])
            with suppress(KeyError):
                self._format_address(director['mailingAddress'])
        return directors

    def _set_addresses(self, filing):
        if filing.get('changeOfAddress'):
            if filing.get('changeOfAddress').get('offices'):
                filing['registeredOfficeAddress'] = filing['changeOfAddress']['offices']['registeredOffice']
                if filing['changeOfAddress']['offices'].get('recordsOffice', None):
                    filing['recordsOfficeAddress'] = filing['changeOfAddress']['offices']['recordsOffice']
                    filing['recordsOfficeAddress']['deliveryAddress'] = \
                        self._format_address(filing['recordsOfficeAddress']['deliveryAddress'])
                    filing['recordsOfficeAddress']['mailingAddress'] = \
                        self._format_address(filing['recordsOfficeAddress']['mailingAddress'])
            else:
                filing['registeredOfficeAddress'] = filing['changeOfAddress']
        else:
            if filing.get('annualReport', {}).get('deliveryAddress'):
                filing['registeredOfficeAddress'] = {
                    'deliveryAddress': filing['annualReport']['deliveryAddress'],
                    'mailingAddress': filing['annualReport']['mailingAddress']
                }
            else:
                filing['registeredOfficeAddress'] = {
                    'deliveryAddress': filing['annualReport']['offices']['registeredOffice']['deliveryAddress'],
                    'mailingAddress': filing['annualReport']['offices']['registeredOffice']['mailingAddress']
                }
        delivery_address = filing['registeredOfficeAddress']['deliveryAddress']
        mailing_address = filing['registeredOfficeAddress']['mailingAddress']
        filing['registeredOfficeAddress']['deliveryAddress'] = self._format_address(delivery_address)
        filing['registeredOfficeAddress']['mailingAddress'] = self._format_address(mailing_address)

    @staticmethod
    def _format_address(address):
        address['streetAddressAdditional'] = address.get('streetAddressAdditional') or ''
        address['addressRegion'] = address.get('addressRegion') or ''
        address['deliveryInstructions'] = address.get('deliveryInstructions') or ''

        country = address['addressCountry']
        country = pycountry.countries.search_fuzzy(country)[0].name
        address['addressCountry'] = country
        return address

    @staticmethod
    def _populate_business_info_to_filing(filing: Filing, business: Business):
        founding_datetime = LegislationDatetime.as_legislation_timezone(business.founding_date)
        if filing.transaction_id:
            business_json = VersionedBusinessDetailsService.get_business_revision(filing.transaction_id, business)
        else:
            business_json = business.json()
        business_json['formatted_founding_date_time'] = LegislationDatetime.format_as_report_string(founding_datetime)
        business_json['formatted_founding_date'] = founding_datetime.strftime(OUTPUT_DATE_FORMAT)
        filing.filing_json['filing']['business'] = business_json
        filing.filing_json['filing']['header']['filingId'] = filing.id

    def _format_transition_data(self, filing):
        filing.update(filing['transition'])
        self._format_directors(filing['parties'])
        if filing.get('shareStructure', {}).get('shareClasses', None):
            filing['shareClasses'] = filing['shareStructure']['shareClasses']

    def _format_incorporation_data(self, filing):
        self._format_address(filing['incorporationApplication']['offices']['registeredOffice']['deliveryAddress'])
        self._format_address(filing['incorporationApplication']['offices']['registeredOffice']['mailingAddress'])
        if 'recordsOffice' in filing['incorporationApplication']['offices']:
            self._format_address(filing['incorporationApplication']['offices']['recordsOffice']['deliveryAddress'])
            self._format_address(filing['incorporationApplication']['offices']['recordsOffice']['mailingAddress'])
        self._format_directors(filing['incorporationApplication']['parties'])
        # create helper lists
        filing['nameRequest'] = filing['incorporationApplication'].get('nameRequest')
        filing['listOfTranslations'] = filing['incorporationApplication'].get('nameTranslations', [])
        filing['offices'] = filing['incorporationApplication']['offices']
        filing['parties'] = filing['incorporationApplication']['parties']
        if filing['incorporationApplication'].get('shareClasses', None):
            filing['shareClasses'] = filing['incorporationApplication']['shareClasses']
        elif 'shareStructure' in filing['incorporationApplication']:
            filing['shareClasses'] = filing['incorporationApplication']['shareStructure']['shareClasses']

        if cooperative := filing['incorporationApplication'].get('cooperative', None):
            cooperative['associationTypeName'] = \
                ASSOCIATION_TYPE_DESC.get(cooperative['cooperativeAssociationType'], '')

    def _format_registration_data(self, filing):
        with suppress(KeyError):
            self._format_address(filing['registration']['offices']['businessOffice']['deliveryAddress'])
        with suppress(KeyError):
            self._format_address(filing['registration']['offices']['businessOffice']['mailingAddress'])
        self._format_directors(filing['registration']['parties'])

        start_date = datetime.fromisoformat(filing['registration']['startDate'])
        filing['registration']['startDate'] = start_date.strftime(OUTPUT_DATE_FORMAT)

    def _format_name_change_data(self, filing):
        meta_data = self._filing.meta_data or {}
        from_legal_name = ''
        to_legal_name = ''
        if self._filing.filing_type == 'alteration':
            from_legal_name = meta_data.get('alteration', {}).get('fromLegalName')
            to_legal_name = meta_data.get('alteration', {}).get('toLegalName')
        if self._filing.filing_type == 'specialResolution' and 'changeOfName' in meta_data.get('legalFilings', []):
            from_legal_name = meta_data.get('changeOfName', {}).get('fromLegalName')
            to_legal_name = meta_data.get('changeOfName', {}).get('toLegalName')
        filing['fromLegalName'] = from_legal_name
        filing['toLegalName'] = to_legal_name

    def _format_certificate_of_restoration_data(self, filing):
        meta_data = self._filing.meta_data or {}
        filing['fromLegalName'] = meta_data.get('restoration', {}).get('fromLegalName')
        filing['toLegalName'] = meta_data.get('restoration', {}).get('toLegalName')
        if expiry_date := meta_data.get('restoration', {}).get('expiry'):
            filing['restoration_expiry_date'] = datetime.fromisoformat(expiry_date).strftime(OUTPUT_DATE_FORMAT)
        if self._filing.filing_sub_type == 'limitedRestorationToFull':
            business_previous_restoration_expiry = \
                VersionedBusinessDetailsService.find_last_value_from_business_revision(self._filing.transaction_id,
                                                                                       self._business.id,
                                                                                       is_restoration_expiry_date=True)
            restoration_expiry_datetime = LegislationDatetime.as_legislation_timezone(
                business_previous_restoration_expiry.restoration_expiry_date)
            filing['previous_restoration_expiry_date'] = restoration_expiry_datetime.strftime(OUTPUT_DATE_FORMAT)

        business_dissolution = VersionedBusinessDetailsService.find_last_value_from_business_revision(
            self._filing.transaction_id, self._business.id, is_dissolution_date=True)
        filing['formatted_dissolution_date'] = \
            LegislationDatetime.format_as_report_string(business_dissolution.dissolution_date)

    def _format_restoration_data(self, filing):
        filing['nameRequest'] = filing['restoration'].get('nameRequest')
        filing['parties'] = filing['restoration'].get('parties')
        filing['offices'] = filing['restoration']['offices']
        meta_data = self._filing.meta_data or {}
        filing['fromLegalName'] = meta_data.get('restoration', {}).get('fromLegalName')
        filing['numberedLegalNameSuffix'] = Business.BUSINESSES[self._business.legal_type]['numberedLegalNameSuffix']

        if relationships := filing['restoration'].get('relationships'):
            filing['relationshipsDesc'] = ', '.join(relationships)

        approval_type = filing['restoration'].get('approvalType')
        filing['approvalType'] = approval_type
        if approval_type == 'courtOrder':
            filing['courtOrder'] = filing['restoration'].get('courtOrder')
        else:
            filing['applicationDate'] = filing['restoration'].get('applicationDate', 'Not Applicable')
            filing['noticeDate'] = filing['restoration'].get('noticeDate', 'Not Applicable')

        business_dissolution = VersionedBusinessDetailsService.find_last_value_from_business_revision(
            self._filing.transaction_id, self._business.id, is_dissolution_date=True)
        filing['dissolutionLegalName'] = business_dissolution.legal_name

        if expiry_date := meta_data.get('restoration', {}).get('expiry'):
            expiry_date = LegislationDatetime.as_legislation_timezone_from_date_str(expiry_date)
            expiry_date = expiry_date.replace(minute=1)
            filing['restoration_expiry_date'] = LegislationDatetime.format_as_report_string(expiry_date)

    def _format_alteration_data(self, filing):
        # Get current list of translations in alteration. None if it is deletion
        if 'nameTranslations' in filing['alteration']:
            filing['listOfTranslations'] = filing['alteration'].get('nameTranslations', [])
            # Get previous translations for deleted translations. No record created in aliases version for deletions
            filing['previousNameTranslations'] = VersionedBusinessDetailsService.get_name_translations_before_revision(
                self._filing.transaction_id, self._business.id)
        if filing['alteration'].get('shareStructure', None):
            filing['shareClasses'] = filing['alteration']['shareStructure'].get('shareClasses', [])
            filing['resolutions'] = filing['alteration']['shareStructure'].get('resolutionDates', [])

        to_legal_name = None
        if self._filing.status == 'COMPLETED':
            meta_data = self._filing.meta_data or {}
            prev_legal_type = meta_data.get('alteration', {}).get('fromLegalType')
            new_legal_type = meta_data.get('alteration', {}).get('toLegalType')
            prev_legal_name = meta_data.get('alteration', {}).get('fromLegalName')
            to_legal_name = meta_data.get('alteration', {}).get('toLegalName')
        else:
            prev_legal_type = filing.get('business').get('legalType')
            new_legal_type = filing.get('alteration').get('business').get('legalType')
            prev_legal_name = filing.get('business').get('legalName')
            identifier = filing.get('business').get('identifier')
            name_request_json = filing.get('alteration').get('nameRequest')
            if name_request_json:
                to_legal_name = name_request_json.get('legalName', identifier[2:] + ' B.C. LTD.')

        if prev_legal_name and to_legal_name and prev_legal_name != to_legal_name:
            filing['previousLegalName'] = prev_legal_name
            filing['newLegalName'] = to_legal_name
        filing['nameRequest'] = filing.get('alteration').get('nameRequest', {})
        filing['provisionsRemoved'] = filing.get('alteration').get('provisionsRemoved')

        filing['previousLegalType'] = prev_legal_type
        filing['newLegalType'] = new_legal_type
        filing['previousLegalTypeDescription'] = self._get_legal_type_description(prev_legal_type)\
            if prev_legal_type else None
        filing['newLegalTypeDescription'] = self._get_legal_type_description(new_legal_type)\
            if new_legal_type else None

    def _format_change_of_registration_data(self, filing, filing_type):  # noqa: E501 # pylint: disable=too-many-locals, too-many-branches, too-many-statements
        prev_completed_filing = Filing.get_previous_completed_filing(self._filing)
        versioned_business = VersionedBusinessDetailsService.\
            get_business_revision_obj(prev_completed_filing.transaction_id, self._business)

        # Change of Name
        prev_legal_name = versioned_business.legal_name
        name_request_json = filing.get(filing_type).get('nameRequest')
        filing['nameRequest'] = name_request_json
        if name_request_json:
            to_legal_name = name_request_json.get('legalName')
            if prev_legal_name and to_legal_name and prev_legal_name != to_legal_name:
                filing['previousLegalName'] = prev_legal_name
                filing['newLegalName'] = to_legal_name

        # Change of Nature of Business
        prev_naics_description = versioned_business.naics_description
        naics_json = filing.get(filing_type).get('business', {}).get('naics')
        if naics_json:
            to_naics_description = naics_json.get('naicsDescription')
            if prev_naics_description and to_naics_description and prev_naics_description != to_naics_description:
                filing['newNaicsDescription'] = to_naics_description

        # Change of start date
        if filing_type == 'correction':
            prev_start_date = versioned_business.start_date
            new_start_date_str = filing.get(filing_type).get('startDate')
            if new_start_date_str:
                new_start_date = datetime.fromisoformat(new_start_date_str) + timedelta(hours=8)
                if prev_start_date != new_start_date:
                    filing['newStartDate'] = new_start_date_str

        # Change of Address
        if business_office := filing.get(filing_type).get('offices', {}).get('businessOffice'):
            filing['offices'] = {}
            filing['offices']['businessOffice'] = business_office
            offices_json = VersionedBusinessDetailsService.get_office_revision(
                prev_completed_filing.transaction_id,
                self._filing.business_id)
            filing['offices']['businessOffice']['mailingAddress']['changed'] = \
                self._compare_address(business_office.get('mailingAddress'),
                                      offices_json['businessOffice']['mailingAddress'])
            filing['offices']['businessOffice']['deliveryAddress']['changed'] = \
                self._compare_address(business_office.get('deliveryAddress'),
                                      offices_json['businessOffice']['deliveryAddress'])
            filing['offices']['businessOffice']['changed'] = \
                filing['offices']['businessOffice']['mailingAddress']['changed']\
                or filing['offices']['businessOffice']['deliveryAddress']['changed']
            with suppress(KeyError):
                self._format_address(filing[filing_type]['offices']['businessOffice']['deliveryAddress'])
            with suppress(KeyError):
                self._format_address(filing[filing_type]['offices']['businessOffice']['mailingAddress'])

        # Change of party
        if filing.get(filing_type).get('parties'):
            filing['parties'] = filing.get(filing_type).get('parties')
            self._format_directors(filing['parties'])
            filing['partyChange'] = False
            filing['newParties'] = []
            parties_to_edit = []
            for party in filing.get('parties'):
                if party['officer'].get('id'):
                    parties_to_edit.append(str(party['officer'].get('id')))
                    prev_party =\
                        VersionedBusinessDetailsService.get_party_revision(
                            prev_completed_filing.transaction_id, party['officer'].get('id'))
                    prev_party_json = VersionedBusinessDetailsService.party_revision_json(
                        prev_completed_filing.transaction_id, prev_party, True)
                    if self._has_party_name_change(prev_party_json, party):
                        party['nameChanged'] = True
                        party['previousName'] = self._get_party_name(prev_party_json)
                        filing['partyChange'] = True
                    if self._compare_address(party.get('mailingAddress'), prev_party_json.get('mailingAddress')):
                        party['mailingAddress']['changed'] = True
                        filing['partyChange'] = True
                    if self._compare_address(party.get('deliveryAddress'), prev_party_json.get('deliveryAddress')):
                        party['deliveryAddress']['changed'] = True
                        filing['partyChange'] = True
                else:
                    if [role for role in party.get('roles', []) if role['roleType'].lower() in ['partner']]:
                        filing['newParties'].append(party)

            existing_party_json = VersionedBusinessDetailsService.get_party_role_revision(
                prev_completed_filing.transaction_id, self._business.id, True)
            parties_deleted = [p for p in existing_party_json if p['officer']['id'] not in parties_to_edit]
            filing['ceasedParties'] = parties_deleted

    @staticmethod
    def _get_party_name(party_json):
        party_name = ''
        if party_json.get('officer').get('partyType') == 'person':
            last_name = party_json['officer'].get('lastName')
            first_name = party_json['officer'].get('firstName')
            middle_initial = party_json['officer'].get('middleInitial')\
                if party_json['officer'].get('middleInitial') else ''
            party_name = f'{last_name}, {first_name} {middle_initial}'
        elif party_json.get('officer').get('partyType') == 'organization':
            party_name = party_json['officer'].get('organizationName')
        return party_name

    @staticmethod
    def _has_party_name_change(prev_party_json, current_party_json):
        changed = False
        middle_name = current_party_json['officer'].get('middleName', current_party_json['officer'].
                                                        get('middleInitial', ''))
        if current_party_json.get('officer').get('partyType') == 'person':
            if prev_party_json['officer'].get('firstName').upper() != current_party_json['officer'].get('firstName').\
                    upper() or prev_party_json['officer'].get('middleName', '').upper() != \
                    middle_name.upper() or prev_party_json['officer'].get('lastName').upper() != \
                    current_party_json['officer'].get('lastName').upper():
                changed = True
        elif current_party_json.get('officer').get('partyType') == 'organization':
            if prev_party_json['officer'].get('organizationName').upper() != \
                    current_party_json['officer'].get('organizationName').upper():
                changed = True
        return changed

    @staticmethod
    def _compare_address(new_address, existing_address):
        if not new_address and not existing_address:
            return False
        if new_address and not existing_address:
            return True

        changed = False
        excluded_keys = ['addressCountryDescription', 'addressType', 'addressCountry']
        for key in existing_address:
            if key not in excluded_keys:
                if (new_address.get(key, '') or '') != (existing_address.get(key) or ''):
                    changed = True
        return changed

    @staticmethod
    def _get_legal_type_description(legal_type):
        corp_type = CorpType.find_by_id(legal_type)
        return corp_type.full_desc if corp_type else None

    def _has_change(self, old_value, new_value):
        """Check to fix the hole in diff.

        example:
            old_value: None and new_value: ''
            In reality there is no change but diff track it as a change
        """
        has_change = True  # assume that in all other cases diff has a valid change
        if isinstance(old_value, str) and new_value is None:
            has_change = old_value != ''
        elif isinstance(new_value, str) and old_value is None:
            has_change = new_value != ''
        elif isinstance(old_value, bool) and new_value is None:
            has_change = old_value is True
        elif isinstance(new_value, bool) and old_value is None:
            has_change = new_value is True

        return has_change

    def _format_correction_data(self, filing):
        prev_completed_filing = Filing.get_previous_completed_filing(self._filing)
        versioned_business = VersionedBusinessDetailsService.\
            get_business_revision_obj(prev_completed_filing.transaction_id, self._business)

        self._format_name_request_data(filing, versioned_business)
        self._format_name_translations_data(filing, prev_completed_filing)
        self._format_office_data(filing, prev_completed_filing)
        self._format_party_data(filing, prev_completed_filing)
        self._format_share_class_data(filing, prev_completed_filing)

    def _format_name_request_data(self, filing, versioned_business: Business):
        name_request_json = filing.get('correction').get('nameRequest', {})
        filing['nameRequest'] = name_request_json
        prev_legal_name = versioned_business.legal_name
        business = VersionedBusinessDetailsService.\
            get_business_revision_obj(self._filing.transaction_id, self._business)
        if prev_legal_name != business.legal_name:
            filing['previousLegalName'] = prev_legal_name
            filing['newLegalName'] = business.legal_name

    def _format_name_translations_data(self, filing, prev_completed_filing: Filing):
        filing['listOfTranslations'] = filing['correction'].get('nameTranslations', [])
        versioned_name_translations = VersionedBusinessDetailsService.\
            get_name_translations_revision(prev_completed_filing.transaction_id, self._business.id)
        filing['previousNameTranslations'] = versioned_name_translations
        filing['nameTranslationsChange'] = sorted(filing['listOfTranslations']) != sorted(versioned_name_translations)

    def _format_office_data(self, filing, prev_completed_filing: Filing):
        filing['offices'] = {}
        if offices := filing.get('correction').get('offices'):
            offices_json = VersionedBusinessDetailsService.get_office_revision(prev_completed_filing.transaction_id,
                                                                               self._filing.business_id)
            if registered_office := offices.get('registeredOffice'):
                filing['offices']['registeredOffice'] = registered_office
                filing['offices']['registeredOffice']['mailingAddress']['changed'] = \
                    self._compare_address(registered_office.get('mailingAddress'),
                                          offices_json['registeredOffice']['mailingAddress'])
                filing['offices']['registeredOffice']['deliveryAddress']['changed'] = \
                    self._compare_address(registered_office.get('deliveryAddress'),
                                          offices_json['registeredOffice']['deliveryAddress'])
                filing['offices']['registeredOffice']['changed'] = \
                    filing['offices']['registeredOffice']['mailingAddress']['changed'] \
                    or filing['offices']['registeredOffice']['deliveryAddress']['changed']
                with suppress(KeyError):
                    self._format_address(filing['offices']['registeredOffice']['deliveryAddress'])
                with suppress(KeyError):
                    self._format_address(filing['offices']['registeredOffice']['mailingAddress'])

            if records_office := offices.get('recordsOffice'):
                filing['offices']['recordsOffice'] = records_office
                filing['offices']['recordsOffice']['mailingAddress']['changed'] = \
                    self._compare_address(records_office.get('mailingAddress'),
                                          offices_json['recordsOffice']['mailingAddress'])
                filing['offices']['recordsOffice']['deliveryAddress']['changed'] = \
                    self._compare_address(records_office.get('deliveryAddress'),
                                          offices_json['recordsOffice']['deliveryAddress'])
                filing['offices']['recordsOffice']['changed'] = \
                    filing['offices']['recordsOffice']['mailingAddress']['changed'] \
                    or filing['offices']['recordsOffice']['deliveryAddress']['changed']
                with suppress(KeyError):
                    self._format_address(filing['offices']['recordsOffice']['deliveryAddress'])
                with suppress(KeyError):
                    self._format_address(filing['offices']['recordsOffice']['mailingAddress'])

    def _format_party_data(self, filing, prev_completed_filing: Filing):
        filing['parties'] = filing.get('correction').get('parties', [])
        if filing.get('parties'):
            self._format_directors(filing['parties'])
            filing['partyChange'] = False
            filing['newParties'] = []
            parties_to_edit = []
            for party in filing.get('parties'):
                if party_id := party['officer'].get('id'):
                    parties_to_edit.append(str(party_id))
                    prev_party =\
                        VersionedBusinessDetailsService.get_party_revision(
                            prev_completed_filing.transaction_id, party_id)
                    prev_party_json = VersionedBusinessDetailsService.party_revision_json(
                        prev_completed_filing.transaction_id, prev_party, True)
                    if self._has_party_name_change(prev_party_json, party):
                        party['nameChanged'] = True
                        party['previousName'] = self._get_party_name(prev_party_json)
                        filing['partyChange'] = True
                    if self._compare_address(party.get('mailingAddress'), prev_party_json.get('mailingAddress')):
                        party['mailingAddress']['changed'] = True
                        filing['partyChange'] = True
                    if self._compare_address(party.get('deliveryAddress'), prev_party_json.get('deliveryAddress')):
                        party['deliveryAddress']['changed'] = True
                        filing['partyChange'] = True
                else:
                    if [role for role in party.get('roles', []) if role['roleType'].lower() in ['director']]:
                        filing['newParties'].append(party)

            existing_party_json = VersionedBusinessDetailsService.get_party_role_revision(
                prev_completed_filing.transaction_id, self._business.id, True)
            parties_deleted = [p for p in existing_party_json if p['officer']['id'] not in parties_to_edit]
            filing['ceasedParties'] = parties_deleted

    def _format_share_class_data(self, filing, prev_completed_filing: Filing):  # pylint: disable=too-many-locals; # noqa: E501;
        filing['shareClasses'] = filing.get('correction').get('shareStructure', {}).get('shareClasses')
        filing['resolutions'] = filing.get('correction').get('shareStructure', {}).get('resolutionDates', [])
        filing['newShareClasses'] = []
        if filing.get('shareClasses'):
            prev_share_class_json = VersionedBusinessDetailsService.get_share_class_revision(
                prev_completed_filing.transaction_id,
                prev_completed_filing.business_id)
            prev_share_class_ids = [x['id'] for x in prev_share_class_json]

            share_class_to_edit = []
            for share_class in filing.get('shareClasses'):
                if share_class_id := share_class.get('id'):
                    if (share_class_id := str(share_class_id)) in prev_share_class_ids:
                        share_class_to_edit.append(share_class_id)
                        if self._compare_json(share_class,
                                              next((x for x in prev_share_class_json if x['id'] == share_class_id)),
                                              ['id', 'series', 'type']):
                            share_class['changed'] = True
                            filing['shareClassesChange'] = True

                        self._format_share_series_data(share_class, filing, prev_completed_filing)
                    else:
                        del share_class['id']
                        filing['newShareClasses'].append(share_class)
                else:
                    filing['newShareClasses'].append(share_class)

            ceased_share_classes = [s for s in prev_share_class_json if s['id'] not in share_class_to_edit]
            filing['ceasedShareClasses'] = ceased_share_classes

    def _format_share_series_data(self, share_class, filing, prev_completed_filing: Filing):  # pylint: disable=too-many-locals; # noqa: E501;
        if share_class.get('series'):
            prev_share_series_json = VersionedBusinessDetailsService.get_share_series_revision(
                prev_completed_filing.transaction_id,
                share_class.get('id'))
            prev_share_series_ids = [x['id'] for x in prev_share_series_json]
            share_series_to_edit = []
            for share_series in share_class.get('series'):
                if share_series_id := share_series.get('id'):
                    if (share_series_id := str(share_series_id)) in prev_share_series_ids:
                        share_series_to_edit.append(share_series_id)
                        if self._compare_json(share_series,
                                              next((x for x in prev_share_series_json if x['id'] == share_series_id)),
                                              ['id', 'type']):
                            share_series['changed'] = True
                            filing['shareClassesChange'] = True
                    else:
                        del share_series['id']
                        filing['shareClassesChange'] = True
                else:
                    filing['shareClassesChange'] = True

            ceased_share_series = [s for s in prev_share_series_json if s['id'] not in share_series_to_edit]
            if ceased_share_series:
                filing['shareClassesChange'] = True

    @staticmethod
    def _compare_json(new_json, existing_json, excluded_keys):
        if not new_json and not existing_json:
            return False
        if new_json and not existing_json:
            return True

        changed = False
        for key in existing_json:
            if key not in excluded_keys:
                if (new_json.get(key, '') or '') != (existing_json.get(key) or ''):
                    changed = True
        return changed

    def _format_special_resolution(self, filing):
        display_name = FILINGS.get(self._filing.filing_type, {}).get('displayName')
        if isinstance(display_name, dict):
            display_name = display_name.get(self._business.legal_type)
        filing['header']['displayName'] = display_name
        resolution_date_str = filing.get('specialResolution', {}).get('resolutionDate', None)
        signing_date_str = filing.get('specialResolution', {}).get('signingDate', None)
        if resolution_date_str:
            resolution_date = datetime.fromisoformat(resolution_date_str)
            filing['specialResolution']['resolutionDate'] = resolution_date.strftime(OUTPUT_DATE_FORMAT)
        if signing_date_str:
            signing_date = datetime.fromisoformat(signing_date_str)
            filing['specialResolution']['signingDate'] = signing_date.strftime(OUTPUT_DATE_FORMAT)

    def _format_noa_data(self, filing):
        filing['header'] = {}
        filing['header']['filingId'] = self._filing.id

    def _set_meta_info(self, filing):
        filing['environment'] = f'{self._get_environment()} FILING #{self._filing.id}'.lstrip()
        # Get source
        filing['source'] = self._filing.source
        # Appears in the Description section of the PDF Document Properties as Title.
        if not (title := self._filing.FILINGS[self._filing.filing_type].get('title')):
            if not (self._filing.filing_sub_type and (title := self._filing.FILINGS[self._filing.filing_type]
                                                      .get(self._filing.filing_sub_type, {})
                                                      .get('title'))):
                title = self._filing.filing_type
        filing['meta_title'] = '{} on {}'.format(title, filing['filing_date_time'])

        # Appears in the Description section of the PDF Document Properties as Subject.
        if self._report_key == 'noticeOfArticles':
            filing['meta_subject'] = '{} ({})'.format(self._business.legal_name, self._business.identifier)
        else:
            legal_name = self._filing.filing_json['filing'].get('business', {}).get('legalName', 'NA')
            filing['meta_subject'] = '{} ({})'.format(
                legal_name,
                self._filing.filing_json['filing'].get('business', {}).get('identifier', 'NA'))

    @staticmethod
    def _get_environment():
        namespace = os.getenv('POD_NAMESPACE', '').lower()
        if namespace.endswith('dev'):
            return 'DEV'
        if namespace.endswith('test'):
            return 'TEST'
        return ''


class ReportMeta:  # pylint: disable=too-few-public-methods
    """Helper class to maintain the report meta information."""

    reports = {
        'certificate': {
            'filingDescription': 'Certificate of Incorporation',
            'fileName': 'certificateOfIncorporation'
        },
        'incorporationApplication': {
            'filingDescription': 'Incorporation Application',
            'fileName': 'incorporationApplication'
        },
        'noticeOfArticles': {
            'filingDescription': 'Notice of Articles',
            'fileName': 'noticeOfArticles'
        },
        'alterationNotice': {
            'filingDescription': 'Alteration Notice',
            'fileName': 'alterationNotice'
        },
        'transition': {
            'filingDescription': 'Transition Application',
            'fileName': 'transitionApplication'
        },
        'changeOfAddress': {
            'hasDifferentTemplates': True,
            'filingDescription': 'Change of Address',
            'default': {
                'fileName': 'bcAddressChange'
            },
            'CP': {
                'fileName': 'changeOfAddress'
            }
        },
        'changeOfDirectors': {
            'hasDifferentTemplates': True,
            'filingDescription': 'Change of Directors',
            'default': {
                'fileName': 'bcDirectorChange'
            },
            'CP': {
                'fileName': 'changeOfDirectors'
            }
        },
        'annualReport': {
            'hasDifferentTemplates': True,
            'filingDescription': 'Annual Report',
            'default': {
                'fileName': 'bcAnnualReport'
            },
            'CP': {
                'fileName': 'annualReport'
            }
        },
        'changeOfName': {
            'filingDescription': 'Change of Name',
            'fileName': 'changeOfName'
        },
        'specialResolution': {
            'filingDescription': 'Special Resolution',
            'fileName': 'specialResolution'
        },
        'voluntaryDissolution': {
            'filingDescription': 'Voluntary Dissolution',
            'fileName': 'voluntaryDissolution'
        },
        'certificateOfNameChange': {
            'filingDescription': 'Certificate of Name Change',
            'fileName': 'certificateOfNameChange'
        },
        'certificateOfDissolution': {
            'filingDescription': 'Certificate of Dissolution',
            'fileName': 'certificateOfDissolution'
        },
        'dissolution': {
            'filingDescription': 'Dissolution Application',
            'fileName': 'dissolution'
        },
        'registration': {
            'filingDescription': 'Statement of Registration',
            'fileName': 'registration'
        },
        'amendedRegistrationStatement': {
            'filingDescription': 'Amended Registration Statement',
            'fileName': 'amendedRegistrationStatement'
        },
        'correctedRegistrationStatement': {
            'filingDescription': 'Corrected Registration Statement',
            'fileName': 'amendedRegistrationStatement'
        },
        'changeOfRegistration': {
            'filingDescription': 'Change of Registration',
            'fileName': 'changeOfRegistration'
        },
        'correction': {
            'hasDifferentTemplates': True,
            'filingDescription': 'Correction',
            'default': {
                'fileName': 'correction'
            },
            'SP': {
                'fileName': 'firmCorrection'
            },
            'GP': {
                'fileName': 'firmCorrection'
            }
        },
        'certificateOfRestoration': {
            'filingDescription': 'Certificate of Restoration',
            'fileName': 'certificateOfRestoration'
        },
        'restoration': {
            'filingDescription': 'Restoration Application',
            'fileName': 'restoration'
        }
    }

    static_reports = {
        'certifiedRules': {
            'documentType': 'coop_rules'
        },
        'certifiedMemorandum': {
            'documentType': 'coop_memorandum'
        },
        'affidavit': {
            'documentType': 'affidavit'
        },
        'uploadedCourtOrder': {
            'documentType': 'court_order'
        }
    }
