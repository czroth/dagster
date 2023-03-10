import datetime
from typing import AbstractSet, Dict, Mapping, Optional, Sequence, cast

import dagster._check as check
from dagster._core.definitions.asset_graph import AssetGraph
from dagster._core.definitions.asset_selection import AssetSelection
from dagster._core.definitions.data_version import get_input_event_pointer_tag
from dagster._core.definitions.events import AssetKey
from dagster._core.definitions.time_window_partitions import (
    TimeWindowPartitionsDefinition,
    TimeWindowPartitionsSubset,
)
from dagster._core.errors import DagsterInvariantViolationError
from dagster._core.event_api import EventLogRecord
from dagster._core.storage.pipeline_run import FINISHED_STATUSES, DagsterRunStatus, RunsFilter
from dagster._utils import frozendict
from dagster._utils.cached_method import cached_method
from dagster._utils.caching_instance_queryer import CachingInstanceQueryer


class CachingDataTimeResolver:
    _instance_queryer: CachingInstanceQueryer
    _asset_graph: AssetGraph

    def __init__(self, instance_queryer: CachingInstanceQueryer, asset_graph: AssetGraph):
        self._instance_queryer = instance_queryer
        self._asset_graph = asset_graph

    @property
    def instance_queryer(self) -> CachingInstanceQueryer:
        return self._instance_queryer

    ####################
    # PARTITIONED DATA TIME
    ####################

    def _calculate_data_time_partitioned(
        self,
        asset_key: AssetKey,
        cursor: int,
        partitions_def: TimeWindowPartitionsDefinition,
    ) -> Optional[datetime.datetime]:
        """Returns the time up until which all available data has been consumed for this asset

        At a high level, this algorithm works as follows:

        First, calculate the subset of partitions that have been materialized up until this point
        in time (ignoring the cursor). This is done by querying the asset status cache if it is
        available, otherwise by using a (slower) get_materialization_count_by_partition query.

        Next, we calculate the set of partitions that are net-new since the cursor. This is done by
        comparing the count of materializations before after the cursor to the total count of
        materializations.

        Finally, we calculate the minimum time window of the net-new partitions. This time window
        did not exist at the time of the cursor, so we know that we have all data up until the
        beginning of that time window, or all data up until the end of the first filled time window
        in the total set, whichever is less.
        """
        from dagster._core.storage.partition_status_cache import (
            get_and_update_asset_status_cache_value,
        )

        if self.instance_queryer.instance.can_cache_asset_status_data():
            # this is the current state of the asset, not the state of the asset at the time of record_id
            status_cache_value = get_and_update_asset_status_cache_value(
                instance=self.instance_queryer.instance,
                asset_key=asset_key,
                partitions_def=partitions_def,
            )
            partition_subset = (
                status_cache_value.deserialize_materialized_partition_subsets(
                    partitions_def=partitions_def
                )
                if status_cache_value
                else partitions_def.empty_subset()
            )
        else:
            # if we can't use the asset status cache, then we get the subset by querying for the
            # existing partitions
            partition_subset = partitions_def.empty_subset().with_partition_keys(
                self._instance_queryer.get_materialized_partitions(asset_key)
            )

        if not isinstance(partition_subset, TimeWindowPartitionsSubset):
            check.failed(f"Invalid partition subset {type(partition_subset)}")

        sorted_time_windows = sorted(partition_subset.included_time_windows)
        # no time windows, no data
        if len(sorted_time_windows) == 0:
            return None
        first_filled_time_window = sorted_time_windows[0]

        first_available_time_window = partitions_def.get_first_partition_window()
        if first_available_time_window is None:
            return None

        # if the first partition has not been filled
        if first_available_time_window.start < first_filled_time_window.start:
            return None

        # there are no events for this asset after the cursor
        asset_record = self._instance_queryer.get_asset_record(asset_key)
        if (
            asset_record is not None
            and asset_record.asset_entry is not None
            and asset_record.asset_entry.last_materialization_record is not None
            and asset_record.asset_entry.last_materialization_record.storage_id <= cursor
        ):
            return first_filled_time_window.end

        # get a per-partition count of the new materializations
        new_partition_counts = self._instance_queryer.get_materialized_partition_counts(
            asset_key, after_cursor=cursor
        )

        total_partition_counts = self._instance_queryer.get_materialized_partition_counts(asset_key)

        # these are the partitions that did not exist before this record was created
        net_new_partitions = {
            partition_key
            for partition_key, new_count in new_partition_counts.items()
            if new_count == total_partition_counts.get(partition_key)
        }

        # there are new materializations, but they don't fill any new partitions
        if not net_new_partitions:
            return first_filled_time_window.end

        # the oldest time window that was newly filled
        oldest_net_new_time_window = min(
            partitions_def.time_window_for_partition_key(partition_key)
            for partition_key in net_new_partitions
        )

        # only factor in the oldest net new time window if it breaks the current first filled time window
        return min(
            oldest_net_new_time_window.start,
            first_filled_time_window.end,
        )

    def _calculate_data_time_by_key_time_partitioned(
        self,
        asset_key: AssetKey,
        cursor: int,
        partitions_def: TimeWindowPartitionsDefinition,
    ) -> Mapping[AssetKey, Optional[datetime.datetime]]:
        """Returns the data time (i.e. the time up to which the asset has incorporated all available
        data) for a time-partitioned asset. This method takes into account all partitions that were
        materialized for this asset up to the provided cursor.
        """
        partition_data_time = self._calculate_data_time_partitioned(
            asset_key=asset_key,
            cursor=cursor,
            partitions_def=partitions_def,
        )

        root_keys = AssetSelection.keys(asset_key).upstream().sources().resolve(self._asset_graph)
        return {key: partition_data_time for key in root_keys}

    ####################
    # UNPARTITIONED DATA TIME
    ####################

    def _upstream_records_by_key(
        self, asset_key: AssetKey, record_id: int, record_tags: Mapping[str, str]
    ) -> Mapping[AssetKey, "EventLogRecord"]:
        upstream_records: Dict[AssetKey, EventLogRecord] = {}

        for parent_key in self._asset_graph.get_parents(asset_key):
            if parent_key in self._asset_graph.source_asset_keys:
                continue

            input_event_pointer_tag = get_input_event_pointer_tag(parent_key)
            if input_event_pointer_tag not in record_tags:
                # if the input event id was not recorded (materialized pre-1.1.0), just grab
                # the most recent asset materialization for this parent which happened before
                # the current record
                parent_record = self._instance_queryer.get_latest_materialization_record(
                    parent_key, before_cursor=record_id
                )
            elif record_tags[input_event_pointer_tag] != "NULL":
                # get the upstream materialization event which was consumed when producing this
                # materialization event
                input_record_id = int(record_tags[input_event_pointer_tag])
                parent_record = self._instance_queryer.get_latest_materialization_record(
                    parent_key, before_cursor=input_record_id + 1
                )
            else:
                parent_record = None

            if parent_record is not None:
                upstream_records[parent_key] = parent_record

        return upstream_records

    @cached_method
    def _calculate_data_time_by_key_unpartitioned(
        self,
        asset_key: AssetKey,
        record_id: int,
        record_timestamp: float,
        record_tags: Mapping[str, str],
    ) -> Mapping[AssetKey, Optional[datetime.datetime]]:
        if not self._asset_graph.has_non_source_parents(asset_key):
            return {
                asset_key: datetime.datetime.fromtimestamp(
                    record_timestamp, tz=datetime.timezone.utc
                )
            }

        data_time_by_key = {}

        # find the upstream times of each of the parents of this asset
        for parent_key, parent_record in self._upstream_records_by_key(
            asset_key, record_id, record_tags
        ).items():
            # recurse to find the data times of this parent
            for upstream_key, data_time in self._calculate_data_time_by_key(
                asset_key=parent_key,
                record_id=parent_record.storage_id if parent_record else None,
                record_timestamp=parent_record.event_log_entry.timestamp if parent_record else 0.0,
                record_tags=frozendict(
                    (
                        parent_record.asset_materialization.tags
                        if parent_record and parent_record.asset_materialization
                        else None
                    )
                    or {}
                ),
            ).items():
                # if root data is missing, this overrides other values
                if data_time is None:
                    data_time_by_key[upstream_key] = None
                else:
                    data_time_by_key[upstream_key] = min(
                        data_time_by_key.get(upstream_key, data_time), data_time
                    )

        return data_time_by_key

    ####################
    # CORE DATA TIME
    ####################

    @cached_method
    def _calculate_data_time_by_key(
        self,
        asset_key: AssetKey,
        record_id: Optional[int],
        record_timestamp: float,
        record_tags: Mapping[str, str],
    ) -> Mapping[AssetKey, Optional[datetime.datetime]]:
        if record_id is None:
            return {key: None for key in self._asset_graph.get_non_source_roots(asset_key)}

        partitions_def = self._asset_graph.get_partitions_def(asset_key)
        if isinstance(partitions_def, TimeWindowPartitionsDefinition):
            return self._calculate_data_time_by_key_time_partitioned(
                asset_key=asset_key,
                cursor=record_id,
                partitions_def=partitions_def,
            )
        else:
            return self._calculate_data_time_by_key_unpartitioned(
                asset_key=asset_key,
                record_id=record_id,
                record_timestamp=record_timestamp,
                record_tags=record_tags,
            )

    ####################
    # IN PROGRESS DATA TIME
    ####################

    @cached_method
    def _get_in_progress_run_ids(self, current_time: datetime.datetime) -> Sequence[str]:
        return [
            record.dagster_run.run_id
            for record in self.instance_queryer.instance.get_run_records(
                filters=RunsFilter(
                    statuses=[
                        status for status in DagsterRunStatus if status not in FINISHED_STATUSES
                    ],
                    # ignore old runs that may be stuck in an unfinished state
                    created_after=current_time - datetime.timedelta(days=1),
                ),
                limit=25,
            )
        ]

    @cached_method
    def _get_in_progress_data_time_in_run(
        self, *, run_id: str, asset_key: AssetKey, current_time: datetime.datetime
    ) -> Optional[datetime.datetime]:
        """Returns the upstream data times that a given asset key will be expected to have at the
        completion of the given run.
        """
        planned_keys = self._instance_queryer.get_planned_materializations_for_run(run_id=run_id)
        materialized_keys = self._instance_queryer.get_current_materializations_for_run(
            run_id=run_id
        )

        # if key is not pending materialization within the run, then downstream assets will generally
        # be expected to consume the current version of the asset
        if asset_key not in planned_keys or asset_key in materialized_keys:
            return self.get_current_data_time(asset_key)

        # if you're here, then this asset is planned, but not materialized. in the worst case, this
        # asset's data time will be equal to the current time once it finishes materializing
        if not self._asset_graph.has_non_source_parents(asset_key):
            return current_time

        data_time = current_time
        for parent_key in self._asset_graph.get_parents(asset_key):
            parent_data_time = self._get_in_progress_data_time_in_run(
                run_id=run_id, asset_key=parent_key, current_time=current_time
            )
            if parent_data_time is None:
                return None

            data_time = min(data_time, parent_data_time)
        return data_time

    def get_in_progress_data_time(
        self, asset_key: AssetKey, current_time: datetime.datetime
    ) -> Optional[datetime.datetime]:
        """Returns a mapping containing the maximum upstream data time that the input asset will
        have once all in-progress runs complete.
        """
        data_time: Optional[datetime.datetime] = None

        for run_id in self._get_in_progress_run_ids(current_time=current_time):
            if not self._instance_queryer.is_asset_planned_for_run(run_id=run_id, asset=asset_key):
                continue

            run_data_time = self._get_in_progress_data_time_in_run(
                run_id=run_id, asset_key=asset_key, current_time=current_time
            )
            if run_data_time is not None:
                data_time = max(run_data_time, data_time or run_data_time)

        return data_time

    ####################
    # FAILED DATA TIME
    ####################

    def get_ignored_failure_data_time(self, asset_key: AssetKey) -> Optional[datetime.datetime]:
        """Returns the data time that this asset would have if the most recent run successfully
        completed. If the most recent run did not fail, then this will return the current data time
        for this asset.
        """
        current_data_time = self.get_current_data_time(asset_key)

        asset_record = self._instance_queryer.get_asset_record(asset_key)

        # no latest run
        if asset_record is None or asset_record.asset_entry.last_run_id is None:
            return current_data_time

        run_id = asset_record.asset_entry.last_run_id
        latest_run_record = self._instance_queryer._get_run_record_by_id(run_id=run_id)

        # latest run did not fail
        if (
            latest_run_record is None
            or latest_run_record.dagster_run.status != DagsterRunStatus.FAILURE
        ):
            return current_data_time

        # run failed, but asset was materialized successfully
        latest_materialization = asset_record.asset_entry.last_materialization
        if (
            latest_materialization is not None
            and latest_materialization.run_id == latest_run_record.dagster_run.run_id
        ):
            return current_data_time

        run_failure_time = datetime.datetime.utcfromtimestamp(
            latest_run_record.end_time or latest_run_record.create_timestamp.timestamp()
        ).replace(tzinfo=datetime.timezone.utc)
        return self._get_in_progress_data_time_in_run(
            run_id=run_id, asset_key=asset_key, current_time=run_failure_time
        )

    ####################
    # MAIN METHODS
    ####################

    def get_data_time_by_key_for_record(
        self,
        record: EventLogRecord,
    ) -> Mapping[AssetKey, Optional[datetime.datetime]]:
        """Method to enable calculating the timestamps of materializations of upstream assets
        which were relevant to a given AssetMaterialization. These timestamps can be calculated relative
        to any upstream asset keys.

        The heart of this functionality is a recursive method which takes a given asset materialization
        and finds the most recent materialization of each of its parents which happened *before* that
        given materialization event.
        """
        if record.asset_key is None or record.asset_materialization is None:
            raise DagsterInvariantViolationError(
                "Can only calculate data times for records with a materialization event and an"
                " asset_key."
            )

        return self._calculate_data_time_by_key(
            asset_key=record.asset_key,
            record_id=record.storage_id,
            record_timestamp=record.event_log_entry.timestamp,
            record_tags=frozendict(record.asset_materialization.tags or {}),
        )

    def get_current_data_time(self, asset_key: AssetKey) -> Optional[datetime.datetime]:
        latest_record = self.instance_queryer.get_latest_materialization_record(asset_key)
        if latest_record is None:
            return None

        data_times = set(self.get_data_time_by_key_for_record(latest_record).values())

        if None in data_times:
            return None

        return min(cast(AbstractSet[datetime.datetime], data_times))

    def get_current_minutes_late(
        self,
        asset_key: AssetKey,
        evaluation_time: datetime.datetime,
    ) -> Optional[float]:
        freshness_policy = self._asset_graph.freshness_policies_by_key.get(asset_key)
        if freshness_policy is None:
            raise DagsterInvariantViolationError(
                "Cannot calculate minutes late for asset without a FreshnessPolicy"
            )

        return freshness_policy.minutes_late(
            data_time=self.get_current_data_time(asset_key),
            evaluation_time=evaluation_time,
        )
