#
#  Copyright 2019 The FATE Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
import os

from fate_arch.common.base_utils import json_dumps, json_loads

from fate_flow.db.db_models import DB, MachineLearningModelInfo as MLModel, PipelineComponentMeta
from fate_flow.model.sync_model import SyncModel
from fate_flow.pipelined_model import pipelined_model
from fate_flow.settings import stat_logger, ENABLE_MODEL_STORE
from fate_flow.utils.base_utils import compare_version
from fate_flow.utils.config_adapter import JobRuntimeConfigAdapter
from fate_flow.utils.model_utils import gather_and_save_model_info, gen_model_id, gen_party_model_id
from fate_flow.utils.job_utils import PIPELINE_COMPONENT_NAME


def compare_roles(request_conf_roles: dict, run_time_conf_roles: dict):
    if request_conf_roles.keys() == run_time_conf_roles.keys():
        verify_format = True
        verify_equality = True
        for key in request_conf_roles.keys():
            verify_format = (
                verify_format and
                len(request_conf_roles[key]) == len(run_time_conf_roles[key]) and
                isinstance(request_conf_roles[key], list)
            )
            request_conf_roles_set = set(str(item) for item in request_conf_roles[key])
            run_time_conf_roles_set = set(str(item) for item in run_time_conf_roles[key])
            verify_equality = verify_equality and (request_conf_roles_set == run_time_conf_roles_set)
        if not verify_format:
            raise Exception("The structure of roles data of local configuration is different from "
                            "model runtime configuration's. Migration aborting.")
        else:
            return verify_equality
    raise Exception("The structure of roles data of local configuration is different from "
                    "model runtime configuration's. Migration aborting.")


def migration(config_data: dict):
    model_id = config_data['model_id']
    model_version = config_data['model_version']
    local_role = config_data['local']['role']
    local_party_id = config_data['local']['party_id']
    unify_model_version = config_data['unify_model_version']

    try:
        if ENABLE_MODEL_STORE:
            sync_model = SyncModel(
                role=local_role, party_id=local_party_id,
                model_id=model_id, model_version=model_version,
            )
            if sync_model.remote_exists():
                sync_model.download(True)

        party_model_id = gen_party_model_id(
            model_id=model_id,
            role=local_role,
            party_id=local_party_id,
        )
        source_model = pipelined_model.PipelinedModel(party_model_id, model_version)
        if not source_model.exists():
            raise FileNotFoundError(f"Can not found {model_id} {model_version} model local cache.")

        with DB.connection_context():
            if MLModel.get_or_none(
                MLModel.f_role == local_role,
                MLModel.f_party_id == local_party_id,
                MLModel.f_model_id == model_id,
                MLModel.f_model_version == unify_model_version,
            ):
                raise FileExistsError(
                    f"Unify model version {unify_model_version} has been occupied in database. "
                     "Please choose another unify model version and try again."
                )

        migrate_tool = source_model.get_model_migrate_tool()
        migrate_model = pipelined_model.PipelinedModel(
            gen_party_model_id(
                model_id=gen_model_id(config_data["migrate_role"]),
                role=local_role,
                party_id=config_data["local"]["migrate_party_id"],
            ),
            unify_model_version,
        )

        query = source_model.pipelined_component.get_define_meta_from_db(
            PipelineComponentMeta.f_component_name != PIPELINE_COMPONENT_NAME,
        )
        for row in query:
            buffer_obj = source_model.read_component_model(row.f_component_name, row.f_model_alias)

            modified_buffer = migrate_tool.model_migration(
                model_contents=buffer_obj,
                module_name=row.f_component_module_name,
                old_guest_list=config_data['role']['guest'],
                new_guest_list=config_data['migrate_role']['guest'],
                old_host_list=config_data['role']['host'],
                new_host_list=config_data['migrate_role']['host'],
                old_arbiter_list=config_data.get('role', {}).get('arbiter', None),
                new_arbiter_list=config_data.get('migrate_role', {}).get('arbiter', None),
            )

            migrate_model.save_component_model(
                row.f_component_name, row.f_component_module_name,
                row.f_model_alias, modified_buffer, row.f_run_parameters,
            )

        pipeline_model = source_model.read_pipeline_model()

        # Utilize Pipeline_model collect model data. And modify related inner information of model
        train_runtime_conf = json_loads(pipeline_model.train_runtime_conf)
        train_runtime_conf["role"] = config_data["migrate_role"]
        train_runtime_conf["initiator"] = config_data["migrate_initiator"]

        adapter = JobRuntimeConfigAdapter(train_runtime_conf)
        train_runtime_conf = adapter.update_model_id_version(model_id=gen_model_id(train_runtime_conf["role"]),
                                                             model_version=migrate_model.model_version)

        # update pipeline.pb file
        pipeline_model.train_runtime_conf = json_dumps(train_runtime_conf, byte=True)
        pipeline_model.model_id = bytes(adapter.get_common_parameters().to_dict().get("model_id"), "utf-8")
        pipeline_model.model_version = bytes(adapter.get_common_parameters().to_dict().get("model_version"), "utf-8")

        if compare_version(pipeline_model.fate_version, '1.5.0') == 'gt':
            pipeline_model.initiator_role = config_data["migrate_initiator"]['role']
            pipeline_model.initiator_party_id = config_data["migrate_initiator"]['party_id']

        # save updated pipeline.pb file
        migrate_model.save_pipeline_model(pipeline_model)

        migrate_model.gen_model_import_config()
        gather_and_save_model_info(migrate_model, local_role, local_party_id)
        migrate_model.packaging_model()

        return (0, f"Migrating model successfully. " \
                  "The configuration of model has been modified automatically. " \
                  "New model id is: {}, model version is: {}. " \
                  "Model files can be found at '{}'.".format(adapter.get_common_parameters().to_dict().get("model_id"),
                                                             migrate_model.model_version,
                                                             os.path.abspath(migrate_model.archive_model_file_path)),
                {"model_id": migrate_model.model_id,
                 "model_version": migrate_model.model_version,
                 "path": os.path.abspath(migrate_model.archive_model_file_path)})
    except Exception as e:
        stat_logger.exception(e)
        return 100, str(e), {}
