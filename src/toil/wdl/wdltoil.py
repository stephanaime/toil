#!/usr/bin/env python3
# Copyright (C) 2018-2022 UCSC Computational Genomics Lab
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import argparse
import collections
import copy
import json
import logging
import os
import subprocess
import sys

from typing import Any, Union, Dict, List, Optional, Set, Iterator

import WDL

from toil.common import Config, Toil, addOptions
from toil.job import Job, JobFunctionWrappingJob, Promise
from toil.fileStores.abstractFileStore import AbstractFileStore

logger = logging.getLogger(__name__)

# Bindings have a long type name
WDLBindings = WDL.Env.Bindings[WDL.Value.Base]

def combine_bindings(all_bindings: List[WDLBindings]) -> WDLBindings:
    """
    Combine variable bindings from multiple predecessor tasks into one set for
    the current task.
    """
    
    # Sort, largest last
    all_bindings = sorted(all_bindings, key=lambda x: len(x))
    
    # Merge them up
    return WDL.Env.merge(*all_bindings)
    
def log_bindings(all_bindings: List[Union[WDLBindings, Promise]]) -> None:
    """
    Log bindings to the console, even if some are still promises.
    """
    for bindings in all_bindings:
        if isinstance(bindings, WDL.Env.Bindings):
            for binding in bindings:
                logger.info("%s = %s", binding.name, binding.value)
        elif isinstance(bindings, UnfulfilledPromiseSentinel):
            logger.info("<Unfulfilled promise for bindings>")

def for_each_node(root: WDL.Tree.WorkflowNode) -> Iterator[WDL.Tree.WorkflowNode]:
    """
    Recursively iterate over all WDL workflow nodes in the given node.
    """
    
    logger.debug('WorkflowNode: %s: %s %s', type(root), root, root.workflow_node_id)
    yield root
    for child_node in root.children:
        if isinstance(child_node, WDL.Tree.WorkflowNode):
            for result in for_each_node(child_node):
                yield result
        else:
            if hasattr(child_node, 'workflow_node_id'):
                logger.debug('Non-WorkflowNode: %s: %s %s', type(child_node), child_node, workflow_node_id)
            else:
                logger.debug('Non-WorkflowNode: %s: %s !!!NO_ID!!!', type(child_node), child_node)
                
class ToilWDLStdLibBase(WDL.StdLib.Base):
    """
    Standard library implementation for WDL as run on Toil.
    """
    
    def __init__(self, file_store: AbstractFileStore):
        """
        Set up the standard library.
        """
        # TODO: Just always be the 1.2 standard library.
        wdl_version = "1.2"
        # Where should we be writing files that write_file() makes?
        write_dir = file_store.getLocalTempDir()
        # Set up miniwdl's implementation (which may be WDL.StdLib.TaskOutputs)
        super().__init__(wdl_version, write_dir)
    
class ToilWDLStdLibTaskOutputs(ToilWDLStdLibBase, WDL.StdLib.TaskOutputs):
    """
    Standard library implementation for WDL as run on Toil, with additional
    functions only allowed in task output sections.
    """
    
    def __init__(self, file_store: AbstractFileStore):
        """
        Set up the standard library for a task output section.
        """
        
        # Just set up as ToilWDLStdLibBase, but it will call into
        # WDL.StdLib.TaskOutputs next.
        super().__init__(file_store)


def evaluate_decl(node: WDL.Tree.Decl, environment: WDLBindings, stdlib: WDL.StdLib.Base) -> WDL.Value.Base:
    """
    Evaluate the expression of a declaration node, or raise an error.
    """
    
    if node.expr is None:
        if node.type.optional:
            # We can just leave the value as null
            value = WDL.Value.Null()
        else:
            raise WDL.Error.EvalError(node, "Cannot evaluate no expression for " + node.name)
    else:
        value = node.expr.eval(environment, stdlib)
    return value

class WDLInputJob(Job):
    """
    Job that evaluates a WDL input, or sources it from the workflow inputs.
    """
    def __init__(self, node: WDL.Tree.Decl, prev_node_results: List[WDLBindings], **kwargs) -> None:
        super().__init__(unitName=node.workflow_node_id, displayName=node.workflow_node_id, **kwargs)
        
        self._node = node
        self._prev_node_results = prev_node_results
        
    def run(self, file_store: AbstractFileStore) -> WDLBindings:
        logger.info("Running node %s", self._node.workflow_node_id)
        
        # Combine the bindings we get from previous jobs
        incoming_bindings = combine_bindings(self._prev_node_results)
        
        if self._node.name in incoming_bindings:
            # This input is user-specified and we don't need to do anything
            logger.debug('Name %s is already defined, not using default', self._node.name)
            return incoming_bindings
        else:
            # We need to evaluate our expression
            # Set up the WDL standard library
            standard_library = ToilWDLStdLibBase(file_store)
            logger.info('Defaulting %s to %s', self._node.name, self._node.expr)
            value = evaluate_decl(self._node, incoming_bindings, standard_library)
            return incoming_bindings.bind(self._node.name, value)
        

class WDLWorkflowNodeJob(Job):
    """
    Job that evaluates a WDL workflow node.
    """
    
    def __init__(self, node: WDL.Tree.WorkflowNode, prev_node_results: List[WDLBindings], **kwargs) -> None:
        super().__init__(unitName=node.workflow_node_id, displayName=node.workflow_node_id, **kwargs)
        
        self._node = node
        self._prev_node_results = prev_node_results
        
    def run(self, file_store: AbstractFileStore) -> WDLBindings:
        logger.info("Running node %s", self._node.workflow_node_id)
        
        # Combine the bindings we get from previous jobs
        incoming_bindings = combine_bindings(self._prev_node_results)
        # Set up the WDL standard library
        standard_library = ToilWDLStdLibBase(file_store)
        
        if isinstance(self._node, WDL.Tree.Decl):
            # This is a variable assignment
            logger.info('Setting %s to %s', self._node.name, self._node.expr)
            value = evaluate_decl(self._node, incoming_bindings, standard_library)    
            return incoming_bindings.bind(self._node.name, value)
        else:
            raise WDL.Error.InvalidType(self._node, "Unimplemented WorkflowNode: " + str(type(self._node)))
        
class WDLSinkJob(Job):
    """
    Job that collects the results from all the WDL workflow nodes that don't
    send results anywhere.
    
    TODO: How to make these be the workflow's result when the workflow source
    node can't claim to return our return value? Move WDL to Toil translation
    into the root job???
    """
    
    def __init__(self, prev_node_results: List[WDLBindings], **kwargs) -> None:
        """
        Make a new sink job to sink the results of all the jobs with no successors themselves.
        """
        super().__init__(**kwargs)
        
        self._prev_node_results = prev_node_results
       
    def run(self, fileStore: AbstractFileStore) -> WDLBindings:
        """
        Aggregate incoming results.
        """
        return combine_bindings(self._prev_node_results)
        
class WDLWorkflowJob(Job):
    """
    Job that evaluates an entire WDL workflow.
    """
    
    def __init__(self, workflow: WDL.Tree.Workflow, prev_node_results: List[WDLBindings], **kwargs) -> None:
        """
        Create a subtree that will run a WDL workflow. The job returns the
        return value of the workflow.
        """
        super().__init__(**kwargs)
        
        # Because we need to return the return value of the workflow, we need
        # to return a Toil promise for the last/sink job in the workflow's
        # graph. But we can't save either a job that takes promises, or a
        # promise, in ourselves, because of the way that Toil resolves promises
        # at deserialization. So we need to do the actual building-out of the
        # workflow in run().
        
        logger.info("Preparing to run workflow %s with inputs:", workflow.name)
        log_bindings(prev_node_results)
        
        self._workflow = workflow
        self._prev_node_results = prev_node_results
        
    def run(self, fileStore: AbstractFileStore) -> Any:
        """
        Run the workflow. Return the result of the workflow.
        """
        
        # Make the incoming bindings environment
        incoming_bindings = combine_bindings(self._prev_node_results)
        
        # What nodes actually participate in dependencies?
        # TODO: The workflow has this already!
        wdl_id_to_wdl_node: Dict[str, WDL.Tree.WorkflowNode] = {node.workflow_node_id: node for n in (self._workflow.inputs + self._workflow.body) for node in for_each_node(n) if isinstance(n, WDL.Tree.WorkflowNode)}
        # What nodes are actually inputs and might not need evaluating?
        wdl_input_ids: Set[str] = {node.workflow_node_id for node in self._workflow.inputs}
        
        # To make Toil jobs, we need all the jobs they depend on made so we can
        # call .rv(). So we need to solve the workflow DAG ourselves to set it up
        # properly.
        
        # What are the dependencies of all the nodes?
        wdl_id_to_dependency_ids = {node_id: node.workflow_node_dependencies for node_id, node in wdl_id_to_wdl_node.items()}
        
        # Which of those are outstanding?
        wdl_id_to_outstanding_dependency_ids = copy.deepcopy(wdl_id_to_dependency_ids)
        
        # What nodes depend on each node?
        wdl_id_to_dependent_ids: Dict[str, Set[str]] = collections.defaultdict(set)
        for node_id, dependencies in wdl_id_to_dependency_ids.items():
            for dependency_id in dependencies:
                # Invert the dependency edges
                wdl_id_to_dependent_ids[dependency_id].add(node_id)
                
        # This will hold all the Toil jobs by WDL node ID
        wdl_id_to_toil_job: Dict[str, Job] = {}
        
        # And collect IDs of jobs with no successors to add a final sink job
        leaf_ids: Set[str] = set()
        
        # What nodes are ready?
        ready_node_ids = {node_id for node_id, dependencies in wdl_id_to_outstanding_dependency_ids.items() if len(dependencies) == 0}
        
        while len(wdl_id_to_outstanding_dependency_ids) > 0:
            logger.debug('Ready nodes: %s', ready_node_ids)
            logger.debug('Waiting nodes: %s', wdl_id_to_outstanding_dependency_ids)
        
            # Find a node that we can do now
            node_id = next(iter(ready_node_ids))
            
            # Say we are doing it
            ready_node_ids.remove(node_id)
            del wdl_id_to_outstanding_dependency_ids[node_id]
            logger.debug('Make Toil job for %s', node_id)
            
            # Collect the return values from previous jobs
            prev_jobs = [wdl_id_to_toil_job[prev_node_id] for prev_node_id in wdl_id_to_dependency_ids[node_id]]
            rvs = [prev_job.rv() for prev_job in prev_jobs]
            if len(rvs) == 0:
                # We also need the initial set of bindings, since they aren't
                # available through a previous job.
                rvs.append(incoming_bindings)
            
            # Use them to make a new job
            if node_id in wdl_input_ids:
                # This is an input.
                assert isinstance(wdl_id_to_wdl_node[node_id], WDL.Tree.Decl)
                # It might be specified, or it might need to be evaluated.
                job = WDLInputJob(wdl_id_to_wdl_node[node_id], rvs)
            else:
                job = WDLWorkflowNodeJob(wdl_id_to_wdl_node[node_id], rvs)
            for prev_job in prev_jobs:
                # Connect up the happens-after relationships to make sure the
                # return values are available.
                # We have a graph that only needs one kind of happens-after
                # relationship, so we always use follow-ons.
                prev_job.addFollowOn(job)

            if len(prev_jobs) == 0:
                # Nothing came before this job, so connect it to the workflow.
                self.addChild(job)
                
            # Save the job
            wdl_id_to_toil_job[node_id] = job
                
            if len(wdl_id_to_dependent_ids[node_id]) == 0:
                # Nothing comes after this job, so connect it to sink
                leaf_ids.add(node_id)
            else:
                for dependent_id in wdl_id_to_dependent_ids[node_id]:
                    # For each job that waits on this job
                    wdl_id_to_outstanding_dependency_ids[dependent_id].remove(node_id)
                    logger.debug('Dependent %s no longer needs to wait on %s', dependent_id, node_id)
                    if len(wdl_id_to_outstanding_dependency_ids[dependent_id]) == 0:
                        # We were the last thing blocking them.
                        ready_node_ids.add(dependent_id)
                        logger.debug('Dependent %s is now ready', dependent_id)
                        
        # Make the sink job
        sink = WDLSinkJob([wdl_id_to_toil_job[node_id].rv() for node_id in leaf_ids])
        # It runs inside us
        self.addChild(sink)
        for node_id in leaf_ids:
            # And after all the leaf jobs.
            wdl_id_to_toil_job[node_id].addFollowOn(sink)
            
        # Return the sink job's return value.
        return sink.rv()

    

def main() -> None:
    """
    A Toil workflow to interpret WDL input files.
    """
    
    parser = argparse.ArgumentParser(description='Runs WDL files with toil.')
    addOptions(parser)
    
    parser.add_argument("wdl_uri", type=str, help="WDL document URI")
    parser.add_argument("inputs_uri", type=str, help="WDL input JSON URI")
    
    options = parser.parse_args(sys.argv[1:])
    
    with Toil(options) as toil:
        if options.restart:
            toil.restart()
        else:
            # Load the WDL document
            document: WDL.Tree.Document = WDL.load(options.wdl_uri)
            
            if document.workflow is None:
                logger.crical("No workflow in document!")
                sys.exit(1)
                
            if document.workflow.inputs:
                # Load the inputs.
                # TODO: Implement URLs
                # TODO: Report good errors
                inputs = json.load(open(options.inputs_uri)) if options.inputs_uri else {}
                # Parse out the available and required inputs. Each key in the
                # JSON ought to start with the workflow's name and then a .
                input_bindings = WDL.values_from_json(inputs, document.workflow.available_inputs, document.workflow.required_inputs, document.workflow.name)
            
            root_job = WDLWorkflowJob(document.workflow, [input_bindings])
            toil.start(root_job)
    
    
    
if __name__ == "__main__":
    main()
     
    
    

